import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from portfolio_lab.config import load_config
from portfolio_lab.demo import create_demo
from portfolio_research.operations import backup_project, restore_backup, sqlite_backup


class BackupTests(unittest.TestCase):
    def test_live_wal_backup_includes_committed_transactions(self):
        with tempfile.TemporaryDirectory() as d:
            source, target = Path(d) / "source.sqlite", Path(d) / "backup.sqlite"
            with sqlite3.connect(source) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE entries (value TEXT)")
                connection.execute("INSERT INTO entries VALUES ('committed')")
                connection.commit()
                sqlite_backup(source, target)
                with sqlite3.connect(target) as backup:
                    self.assertEqual(
                        backup.execute("SELECT value FROM entries").fetchone()[0], "committed"
                    )
                with self.assertRaises(ValueError):
                    sqlite_backup(source, target)

    def test_complete_backup_restore_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = load_config(create_demo(root / "demo"))
            pairing = root / "demo" / ".chrome-connector-key"
            pairing.write_text("synthetic-pairing-fixture")
            target = backup_project(config, root / "backup", root / "demo")
            restored = restore_backup(target, root / "restored")
            restored_config = load_config(restored / "research-config.json")
            self.assertEqual(
                (restored / ".chrome-connector-key").read_text(), "synthetic-pairing-fixture"
            )
            self.assertTrue(Path(restored_config["source"]["path"]).is_file())
            self.assertTrue(Path(restored_config["data"]["prices_csv"]).is_file())
            with self.assertRaises(FileExistsError):
                restore_backup(target, restored)

    def test_corrupt_backup_is_rejected_before_destination_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "backup"
            source.mkdir()
            (source / "value.txt").write_text("tampered")
            (source / "manifest.json").write_text(
                json.dumps({"version": 1, "files": {"value.txt": "0" * 64}})
            )
            with self.assertRaisesRegex(ValueError, "checksum"):
                restore_backup(source, root / "restored")
            self.assertFalse((root / "restored").exists())

    def test_alias_traversal_and_output_collisions_fail_before_restore(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            backup = root / "backup"
            backup.mkdir()
            (backup / "holdings.sqlite3").write_bytes(b"synthetic holdings")
            (backup / "research.sqlite3").write_bytes(b"synthetic research")

            def digest(name):
                return hashlib.sha256((backup / name).read_bytes()).hexdigest()

            cases = [
                {"version": 1, "files": {"../backup/holdings.sqlite3": digest("holdings.sqlite3")}},
                {
                    "version": 1,
                    "source_filename": "research.sqlite3",
                    "files": {
                        "holdings.sqlite3": digest("holdings.sqlite3"),
                        "research.sqlite3": digest("research.sqlite3"),
                    },
                },
            ]
            for manifest in cases:
                with self.subTest(manifest=manifest):
                    (backup / "manifest.json").write_text(json.dumps(manifest))
                    with self.assertRaises(ValueError):
                        restore_backup(backup, root / "another" / "restored")
                    self.assertFalse((root / "another").exists())

    def test_symlinked_support_directory_is_not_backed_up(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = load_config(create_demo(root / "demo"))
            outside = root / "outside"
            outside.mkdir()
            research = Path(config["research"]["path"]).parent
            research.mkdir(exist_ok=True, parents=True)
            (research / "cache").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                backup_project(config, root / "backup", root / "demo")
