"""Consistent SQLite backups and non-destructive restores into new directories."""

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


def sqlite_backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or source == destination:
        raise ValueError("Backup destination must be a new file.")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(destination) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed.")
    destination.chmod(0o600)


def backup_project(config, destination, collector_directory):
    target = Path(destination).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    files = {}
    for key, filename in (("source", "holdings.sqlite3"), ("research", "research.sqlite3")):
        path = Path(config[key]["path"])
        if path.is_file():
            sqlite_backup(path, target / filename)
            files[filename] = hashlib.sha256((target / filename).read_bytes()).hexdigest()
    collector_directory = Path(collector_directory).resolve()
    pairing = collector_directory / ".chrome-connector-key"
    if pairing.is_file():
        shutil.copy2(pairing, target / ".chrome-connector-key")
        (target / ".chrome-connector-key").chmod(0o600)
        files[".chrome-connector-key"] = hashlib.sha256(
            (target / ".chrome-connector-key").read_bytes()
        ).hexdigest()
    (target / "research-config.json").write_text(json.dumps(config, indent=2) + "\n")
    (target / "research-config.json").chmod(0o600)
    files["research-config.json"] = hashlib.sha256(
        (target / "research-config.json").read_bytes()
    ).hexdigest()
    research_directory = Path(config["research"]["path"]).parent
    for name in ("inputs", "cache"):
        source = research_directory / name
        if source.is_symlink():
            raise ValueError("Backup refuses symlinked input/cache directories.")
        if source.is_dir():
            for path in source.rglob("*"):
                if path.is_symlink():
                    raise ValueError(
                        "Backup refuses symlinked input/cache entries; review them separately."
                    )
                if path.is_file():
                    relative = Path(name) / path.relative_to(source)
                    output = target / relative
                    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(path, output)
                    output.chmod(0o600)
                    files[str(relative)] = hashlib.sha256(output.read_bytes()).hexdigest()
    for key, value in config["data"].items():
        if key.endswith("_csv") and value:
            path = Path(value)
            if not path.is_file() or path.is_symlink():
                raise ValueError(
                    "A configured CSV input is missing or symlinked; backup is incomplete."
                )
            output = target / "inputs" / path.name
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if output.exists() and hashlib.sha256(output.read_bytes()).hexdigest() != digest:
                raise ValueError(
                    "Different CSV inputs have colliding filenames; use unique input filenames."
                )
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, output)
            output.chmod(0o600)
            files[str(output.relative_to(target))] = digest
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source_filename": Path(config["source"]["path"]).name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": files,
            },
            indent=2,
        )
        + "\n"
    )
    return target


def restore_backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unsupported backup manifest.")
    source_filename = manifest.get("source_filename", "portfolio.sqlite3")
    if (
        not isinstance(source_filename, str)
        or Path(source_filename).name != source_filename
        or source_filename in {"", ".", ".."}
    ):
        raise ValueError("Invalid backup source filename.")
    outputs = {}
    for relative, digest in manifest["files"].items():
        if (
            not isinstance(relative, str)
            or "\\" in relative
            or "\x00" in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError("Unsafe backup entry path.")
        path = source / relative
        if (
            path.is_symlink()
            or any(
                parent.is_symlink()
                for parent in path.parents
                if parent != source and parent.is_relative_to(source)
            )
            or not path.resolve().is_relative_to(source)
            or not path.is_file()
        ):
            raise ValueError("Unsafe or missing backup entry.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Backup checksum mismatch; no restore was performed.")
        name = source_filename if relative == "holdings.sqlite3" else relative
        output = destination / name
        if not output.resolve().is_relative_to(destination) or output in outputs:
            raise ValueError("Backup entries collide or escape the restore directory.")
        outputs[output] = path
    if any(parent in outputs for output in outputs for parent in output.parents):
        raise ValueError("A backup file conflicts with another entry's directory.")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for target, original in outputs.items():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(original, target)
        target.chmod(0o600)
    config_path = destination / "research-config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        config["source"]["path"] = str(destination / source_filename)
        config["research"] = {
            "path": str(destination / "research.sqlite3"),
            "output_dir": str(destination / "reports"),
        }
        for key, value in list(config["data"].items()):
            if key.endswith("_csv") and value:
                restored = destination / "inputs" / Path(value).name
                config["data"][key] = str(restored) if restored.is_file() else None
        config_path.write_text(json.dumps(config, indent=2) + "\n")
    return destination
