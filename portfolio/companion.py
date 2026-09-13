"""Authenticated bridge to an extension in the user's ordinary Chrome browser."""

import copy
import secrets
import threading
import time
from pathlib import Path

from .storage import yahoo_url
from .yahoo import discover_links


class ChromeCompanion:
    def __init__(self, store):
        self.store = store
        self.store.abandon_batches()
        self.key_path = store.directory / ".chrome-connector-key"
        if not self.key_path.exists():
            self.key_path.write_text(secrets.token_urlsafe(32))
            self.key_path.chmod(0o600)
        self.key = self.key_path.read_text().strip()
        self.lock = threading.RLock()
        self.last_seen = 0
        self.extension_version = None
        self.jobs = []
        self.state = dict(
            connector="chrome",
            busy=False,
            browser_open=False,
            action=None,
            message="Install and pair the Chrome extension, then open Yahoo to sign in normally.",
            discovered=[],
            discovery_warning=None,
            results=[],
            error=None,
            setup_required=False,
            collection_method="holdings-table-v1",
        )

    def authenticate(self, key):
        return bool(key) and secrets.compare_digest(key, self.key)

    def status(self):
        with self.lock:
            self._expire()
            result = copy.deepcopy(self.state)
            result.update(
                browser_open=time.time() - self.last_seen < 90,
                pairing_key=self.key,
                extension_dir=str(Path(__file__).resolve().parent.parent / "chrome-extension"),
            )
            result["extension_version"] = self.extension_version
            result["progress"] = {
                "total": len(self.jobs),
                "completed": sum(j["status"] in ("done", "failed") for j in self.jobs),
                "running_source_id": next(
                    (j["source_id"] for j in self.jobs if j["status"] == "running"), None
                ),
            }
            return result

    def _expire(self):
        running = any(j["status"] == "running" for j in self.jobs)
        idle_since = max((j.get("finished", j["created"]) for j in self.jobs), default=time.time())
        for job in self.jobs:
            deadline_start = (
                job.get("started", job["created"]) if job["status"] == "running" else idle_since
            )
            if (
                job["status"] == "running" or (job["status"] == "pending" and not running)
            ) and time.time() - deadline_start > 300:
                self._fail(
                    job,
                    "Chrome did not finish this request. Open the extension, check its status, and retry.",
                )
        self.state["busy"] = any(j["status"] in ("pending", "running") for j in self.jobs)

    def _fail(self, job, message):
        job["status"] = "failed"
        job["finished"] = time.time()
        job["result"] = {"ok": False, "error": message}
        if job.get("source_id"):
            self.store.failed(job["source_id"], message, batch_id=job.get("batch_id"))
            self.state["results"].append(
                dict(source_id=job["source_id"], name=job["name"], ok=False, error=message)
            )
        self.state.update(error=message, message=message)

    def select_source(self, source_id, selected):
        with self.lock:
            self._expire()
            if self.state["busy"]:
                raise ValueError("Wait for the current pull to finish before changing selection.")
            self.store.select_source(source_id, selected)

    def submit(self, action, source_ids=None):
        with self.lock:
            self._expire()
            if action == "disconnect":
                for job in self.jobs:
                    if job["status"] in ("pending", "running"):
                        self._fail(job, "Chrome connection was disconnected.")
                self.key = secrets.token_urlsafe(32)
                self.key_path.write_text(self.key)
                self.key_path.chmod(0o600)
                self.last_seen = 0
                self.state.update(
                    busy=False,
                    error=None,
                    message="Extension disconnected. Yahoo remains signed in in Chrome.",
                )
                return
            if action not in ("connect", "discover", "refresh"):
                raise ValueError("Unknown browser action.")
            if self.state["busy"]:
                raise ValueError("A Chrome request is already running.")
            sources = []
            if action == "refresh":
                sources = [self.store.source(i) for i in dict.fromkeys(source_ids or [])]
                if not sources or any(not s["url"] or not s["selected"] for s in sources):
                    raise ValueError("Select at least one portfolio with a Yahoo URL.")
            self.jobs = []
            batch_id = (
                self.store.begin_batch([source["id"] for source in sources]) if sources else None
            )
            for source in sources or [None]:
                self.jobs.append(
                    dict(
                        id=secrets.token_hex(16),
                        action=action,
                        status="pending",
                        created=time.time(),
                        url=source["url"] if source else "https://finance.yahoo.com/portfolios/",
                        navigation_url=(source.get("navigation_url") or source["url"] + "/view")
                        if source
                        else "https://finance.yahoo.com/portfolios/",
                        source_id=source["id"] if source else None,
                        batch_id=batch_id,
                        name=source["name"] if source else "Yahoo Finance",
                    )
                )
            self.state.update(
                busy=True,
                action=action,
                results=[],
                error=None,
                message="Waiting for Chrome. Open the extension and click Check now, or allow up to 30 seconds.",
            )

    def take(self, extension_version=None):
        with self.lock:
            self.extension_version = (
                str(extension_version)[:30] if extension_version else "older / unknown"
            )
            self.last_seen = time.time()
            self._expire()
            if any(j["status"] == "running" for j in self.jobs):
                return None
            for job in self.jobs:
                if job["status"] == "pending":
                    job["status"] = "running"
                    job["started"] = time.time()
                    self.state["message"] = "Chrome is opening " + job["name"] + "…"
                    return {
                        **{
                            key: job[key]
                            for key in (
                                "id",
                                "action",
                                "url",
                                "navigation_url",
                                "source_id",
                                "name",
                            )
                        },
                        "collection_method": "holdings-table-v1",
                    }
            return None

    def complete(self, payload):
        with self.lock:
            self.last_seen = time.time()
            self._expire()
            job = next((j for j in self.jobs if j["id"] == payload.get("id")), None)
            if not job:
                raise ValueError("Unknown or expired Chrome job.")
            if job["status"] in ("done", "failed"):
                return job["result"]
            if job["status"] != "running":
                raise ValueError("Chrome has not claimed this job.")
            try:
                if payload.get("ok") is not True:
                    raise ValueError(str(payload.get("error") or "Chrome request failed.")[:700])
                result = {"ok": True}
                if job["action"] == "refresh":
                    if yahoo_url(payload.get("url", "")) != job["url"]:
                        raise ValueError(
                            "Chrome navigated away from the selected portfolio. Previous data was kept."
                        )
                    result.update(
                        self.store.ingest_table(
                            job["source_id"], payload.get("table"), batch_id=job.get("batch_id")
                        )
                    )
                    self.state["results"].append(
                        dict(name=job["name"], source_id=job["source_id"], **result)
                    )
                elif job["action"] == "discover":
                    links = payload.get("links")
                    if (
                        not isinstance(links, list)
                        or len(links) > 2000
                        or any(not isinstance(x, dict) for x in links)
                    ):
                        raise ValueError("Invalid portfolio discovery response.")
                    found = discover_links(links)
                    if not found:
                        raise ValueError(
                            "No portfolio links found. Finish Yahoo sign-in in Chrome, then retry."
                        )
                    names = payload.get("portfolio_names", [])
                    if (
                        not isinstance(names, list)
                        or len(names) > 2000
                        or any(not isinstance(n, str) for n in names)
                    ):
                        raise ValueError("Invalid portfolio table response.")
                    identified = {item["name"].strip() for item in found}
                    # Include dropped link labels even when table metadata is unavailable.
                    candidates = names or [str(item.get("text", "")).strip() for item in links]
                    missing = list(
                        dict.fromkeys(
                            n.strip()[:200]
                            for n in candidates
                            if n.strip() and n.strip() not in identified
                        )
                    )
                    self.state["discovered"] = found
                    self.store.remember_portfolios(found)
                    self.state["discovery_warning"] = (
                        "Discovery is incomplete. These Yahoo entries were not identified: "
                        + "; ".join(missing)
                        + ". Retry Find portfolios after Yahoo finishes loading, or report them."
                        if missing
                        else None
                    )
                job.update(status="done", result=result, finished=time.time())
            except (ValueError, TypeError, AttributeError) as exc:
                self._fail(job, str(exc))
            self._expire()
            if not self.state["busy"]:
                if self.state["results"]:
                    count = sum(item["ok"] for item in self.state["results"])
                    self.state["message"] = (
                        f"Refresh finished: {count} of {len(self.state['results'])} portfolios imported."
                    )
                elif not self.state["error"]:
                    self.state["message"] = (
                        "Yahoo is open in your normal Chrome browser. Complete Google sign-in there, "
                        "then click Find portfolios."
                        if job["action"] == "connect"
                        else (
                            self.state["discovery_warning"]
                            or f"Found {len(self.state['discovered'])} portfolios. Check the ones to include, then pull latest holdings."
                        )
                    )
            return job["result"]
