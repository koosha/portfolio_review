"""Optional summarization provider boundary; no provider implementation ships here.

The default is a null adapter: automatic research stays deterministic unless the
owner configures a runtime provider explicitly. A configured provider receives
only public documents and the minimal identifiers needed to read them, never
balances, positions, account facts or credentials, and its output is a *proposal*
carrying `origin: "llm"` and source citations. All financial arithmetic stays in
Python: an adapter may not compute returns, valuations or weights.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Mapping, Sequence
from typing import Any

INTERFACE_VERSION = "model-adapter-1"
IDENTIFIER_FIELDS = ("symbol", "name")


class NullModelAdapter:
    """The configured-by-default adapter: it summarizes nothing and claims nothing."""

    interface_version = INTERFACE_VERSION
    provider = "null"

    def summarize(self, documents: Sequence, identifiers: Mapping) -> None:
        """Return None; deterministic templates produce the brief instead.

        documents: public research documents already retained with their sources.
        identifiers: minimal public identifiers only (symbol, name).
        A real adapter returns a proposals dict whose entries carry
        `origin: "llm"` and a `source_id` naming retained evidence.
        """
        return None


def public_identifiers(security: Mapping) -> dict:
    """Minimal public identifiers a provider may receive; nothing else leaves the app."""
    if not isinstance(security, Mapping):
        raise ValueError("security must be an object")
    return {field: security.get(field) for field in IDENTIFIER_FIELDS}


def _import_target(path: str) -> Any:
    module_path, separator, attribute = path.rpartition(":")
    if not separator:
        module_path, separator, attribute = path.rpartition(".")
    if not separator or not module_path or not attribute:
        raise ValueError(
            f"data.model_provider must name an importable class as module.Class: {path!r}"
        )
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ValueError(f"data.model_provider {path!r} is not importable: {exc}") from exc
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise ValueError(f"data.model_provider {path!r} does not name a class: {exc}") from exc


def load_model_adapter(config: Mapping | None = None):
    """Return the configured adapter, defaulting to NullModelAdapter.

    `config["data"]["model_provider"]` is either None (the default) or an
    importable `module.Class` / `module:Class` path whose class provides
    `summarize(documents, identifiers)`. Anything else is an invalid supplied
    value and raises ValueError rather than silently degrading to the null adapter.
    """
    data = config.get("data") if isinstance(config, Mapping) else None
    name = data.get("model_provider") if isinstance(data, Mapping) else None
    if name is None:
        return NullModelAdapter()
    if not isinstance(name, str) or not name.strip():
        raise ValueError("data.model_provider must be null or an importable class path")
    target = _import_target(name.strip())
    if not inspect.isclass(target):
        raise ValueError(f"data.model_provider {name!r} must name a class")
    summarize = getattr(target, "summarize", None)
    if not callable(summarize):
        raise ValueError(
            f"data.model_provider {name!r} must provide summarize(documents, identifiers)"
        )
    try:
        adapter = target()
    except TypeError as exc:
        raise ValueError(f"data.model_provider {name!r} could not be constructed: {exc}") from exc
    return adapter
