"""Deliberate browser/report schema; reproducibility internals stay in SQLite."""

import re
from copy import deepcopy
from urllib.parse import urlsplit

from portfolio_lab.analytics import json_safe
from portfolio_lab.config import EDITABLE_GROUPS

RESULT_FIELDS = {
    "run_id",
    "metadata",
    "summary",
    "issues",
    "quality_summary",
    "holdings",
    "issuer_exposure",
    "sector_exposure",
    "exposure_status",
    "signals",
    "risk",
    "scenarios",
    "macro",
    "allocation",
    "proposals",
    "decisions",
    "forecast_inputs",
    "company_research",
    "timeline",
    "comparison",
    "prospective",
    "input_status",
    "observations",
}
SOURCE_FIELDS = {"source_id", "provider", "received_at", "available_at", "sha256", "rows", "status"}
PRIVATE_KEYS = {
    "saved_config",
    "input_manifest",
    "raw_path",
    "path",
    "filename",
    "cache_path",
    "raw_csv",
    "raw_data",
    "positions_query",
    "accounts_query",
    "securities_query",
    "tax_lots_query",
    "sec_user_agent",
    "api_key",
    "token",
    "pairing_key",
    "key",
    "authorization",
    "proxy_authorization",
    "cookie",
    "cookies",
    "set_cookie",
    "auth",
    "headers",
}


def _safe_text(text):
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    # Any absolute local path, whatever it is rooted at: a data directory may sit on an
    # external volume or under /opt, /Library or /etc, and none of those may reach a
    # response. A path inside a URL is left alone: the character before it rules out one
    # preceded by a word character, a colon or another slash.
    text = re.sub(
        r"(?<![\w:/])(?:file://)?/(?:[^/\s\r\n\"'<>][^/\r\n\"'<>]*/)+[^\r\n\"'<>]*",
        "[local path]",
        text,
    )
    text = re.sub(r"\b[A-Za-z]:\\[^\r\n\"'<>]+", "[local path]", text)
    text = re.sub(
        r"(?i)(?:api_?key|token|secret|password)=([^&\s]+)", "credential=[redacted]", text
    )
    return text


def _clean(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key = str(key)
            normalized = key.lower().replace("-", "_")
            if (
                normalized in PRIVATE_KEYS
                or any(
                    word in normalized
                    for word in ("password", "secret", "api_key", "apikey", "credential", "sql")
                )
                or normalized.endswith(("_path", "_query", "_token"))
            ):
                continue
            if _safe_text(key) != key:
                continue
            result[key] = _clean(item)
        return result
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def public_config(config):
    return _clean({key: deepcopy(config[key]) for key in EDITABLE_GROUPS if key in config})


def public_sources(sources):
    records = []
    for source in sources:
        record = {key: source[key] for key in SOURCE_FIELDS if key in source}
        locator = source.get("url") or source.get("locator")
        if isinstance(locator, str):
            try:
                parsed = urlsplit(locator)
            except ValueError:
                records.append(_clean(record))
                continue
            if (
                parsed.scheme == "https"
                and parsed.hostname
                in {"www.sec.gov", "data.sec.gov", "fred.stlouisfed.org", "api.stlouisfed.org"}
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
            ):
                record["locator"] = locator
        records.append(_clean(record))
    return records


def public_result(result):
    selected = {key: deepcopy(result[key]) for key in RESULT_FIELDS if key in result}
    selected["sources"] = public_sources(result.get("sources", []))
    selected["schema_version"] = 1
    return json_safe(_clean(selected))


def public_workspace(workspace):
    return json_safe(
        _clean({key: deepcopy(workspace.get(key, {})) for key in ("assessments", "valuations")})
    )
