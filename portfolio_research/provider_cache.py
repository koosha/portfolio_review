"""One scan of an archived provider cache: the newest stored response per key.

``portfolio_lab.providers._cached`` re-reads every sidecar of a provider on each
lookup, which is quadratic when one presentation reads hundreds of symbols or looks
for a window it did not archive itself. This index reads a provider directory once
and keeps the same rules: a sidecar is data, never permission to read another
directory, and a payload must match the digest recorded beside it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path

_ACTIVE = threading.local()


def cache_root(config, provider: str) -> Path:
    """The provider's cache directory beside the research database."""
    return (
        Path(config.get("research", {}).get("path", "research.sqlite"))
        .expanduser()
        .resolve()
        .parent
        / "cache"
        / provider
    )


def _newer(source, previous) -> bool:
    if previous is None:
        return True
    return (source.get("vintage_date") or "", source["received_at"]) >= (
        previous.get("vintage_date") or "",
        previous["received_at"],
    )


def newest_sources(config, provider: str) -> dict[str, dict]:
    """Newest archived sidecar per cache key, from a single directory scan."""
    root = cache_root(config, provider)
    newest: dict[str, dict] = {}
    if not root.is_dir():
        return newest
    for path in root.glob("*.source.json"):
        if path.is_symlink():
            continue
        try:
            source = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(source, dict):
            continue
        key, received_at = source.get("key"), source.get("received_at")
        if not isinstance(key, str) or not isinstance(received_at, str):
            continue
        if _newer(source, newest.get(key)):
            newest[key] = source
    return newest


def read_payload(config, provider: str, source: dict):
    """``(payload, received_at, source)`` of one archived response, hash-checked."""
    root = cache_root(config, provider)
    path = root / Path(str(source.get("raw_path", ""))).name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("Cached payload path escapes its provider cache")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source.get("sha256"):
        raise ValueError("Cached payload failed its content hash")
    return json.loads(raw), source["received_at"], source


@contextmanager
def scanned(config, *providers: str):
    """Index these provider caches once for the duration of the block.

    Only for cache-only reads: a refresh writes new archives, which this index would
    not see. Nested scans are restored on exit and never cross threads.
    """
    previous = getattr(_ACTIVE, "index", None)
    _ACTIVE.index = {provider: newest_sources(config, provider) for provider in providers}
    try:
        yield _ACTIVE.index
    finally:
        _ACTIVE.index = previous


def scanning(provider: str) -> bool:
    """Whether a scan of this provider is active on this thread."""
    index = getattr(_ACTIVE, "index", None)
    return isinstance(index, dict) and provider in index


def scanned_sources(provider: str) -> dict | None:
    """The active scan's whole index for a provider, or ``None`` when none is open."""
    index = getattr(_ACTIVE, "index", None)
    if not isinstance(index, dict):
        return None
    return index.get(provider)


def scanned_source(provider: str, key: str) -> dict | None:
    """The newest sidecar for a key from the active scan, or ``None``."""
    index = getattr(_ACTIVE, "index", None)
    if not isinstance(index, dict):
        return None
    return index.get(provider, {}).get(key)


def cached_response(config, provider: str, key: str):
    """``(payload, received_at, source)`` for a key, from an open scan when there is one."""
    from portfolio_lab import providers

    if not scanning(provider):
        return providers._cached(config, provider, key)
    source = scanned_source(provider, key)
    if source is None:
        raise FileNotFoundError("No compatible cached provider response")
    return read_payload(config, provider, source)
