import argparse
import json
import mimetypes
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .companion import ChromeCompanion
from .storage import MAX_FILE_BYTES, Store

STATIC = Path(__file__).resolve().parent.parent / "static"


def make_handler(store, browser, port, research=None):
    token = secrets.token_urlsafe(32)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def extension_origin(self):
            origin = self.headers.get("Origin", "")
            return origin if re.fullmatch(r"chrome-extension://[a-p]{32}", origin) else None

        def log_message(self, format, *args):
            pass  # Portfolio names, filenames, and request data stay out of logs.

        def reply(self, status, payload, content_type="application/json", attachment=None):
            data = json.dumps(payload).encode() if content_type == "application/json" else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if urlsplit(self.path).path.startswith("/api/companion/") and self.extension_origin():
                self.send_header("Access-Control-Allow-Origin", self.extension_origin())
                self.send_header("Vary", "Origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(data)

        def trusted(self, mutation=False):
            actual_port = self.server.server_address[1]
            local_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
            local_origins = {"http://" + host for host in local_hosts}
            if self.headers.get("Host") not in local_hosts:
                self.reply(403, {"error": "Localhost access only."})
                return False
            if urlsplit(self.path).path.startswith("/api/companion/"):
                if not isinstance(browser, ChromeCompanion) or not browser.authenticate(
                    self.headers.get("X-Companion-Key", "")
                ):
                    self.reply(
                        403,
                        {
                            "error": "Pair the extension using the connection key shown in the local app."
                        },
                    )
                    return False
                origin = self.headers.get("Origin")
                if origin and not self.extension_origin() and origin not in local_origins:
                    self.reply(403, {"error": "This origin cannot use the Chrome connector."})
                    return False
                return True
            origin = self.headers.get("Origin")
            if (origin and origin not in local_origins) or self.headers.get(
                "Sec-Fetch-Site"
            ) == "cross-site":
                self.reply(403, {"error": "Cross-site access denied."})
                return False
            if mutation and not secrets.compare_digest(
                self.headers.get("X-Local-Token", ""), token
            ):
                self.reply(403, {"error": "Reload the local app before making changes."})
                return False
            return True

        def do_OPTIONS(self):
            if (
                self.headers.get("Host") not in hosts
                or not self.extension_origin()
                or not urlsplit(self.path).path.startswith("/api/companion/")
            ):
                self.reply(403, {"error": "Cross-site access denied."})
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.extension_origin())
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type, X-Companion-Key, X-Companion-Version"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self.trusted():
                return
            path = urlsplit(self.path).path
            try:
                if path in ("/", "/app.js", "/style.css", "/research.js", "/research-state.js"):
                    filename = "index.html" if path == "/" else path[1:]
                    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    self.reply(200, (STATIC / filename).read_bytes(), mime + "; charset=utf-8")
                elif research and path.startswith("/api/research"):
                    self.research_get(path)
                elif path == "/api/state":
                    self.reply(
                        200, dict(token=token, sources=store.sources(), browser=browser.status())
                    )
                elif path == "/api/companion/job":
                    self.reply(200, {"job": browser.take(self.headers.get("X-Companion-Version"))})
                elif path == "/api/export":
                    self.reply(200, store.export(), attachment="portfolio-latest.json")
                elif path == "/api/positions":
                    self.reply(200, {"positions": store.positions()})
                elif path.startswith("/api/sources/") and path.endswith("/snapshots"):
                    self.reply(200, store.snapshots(int(path.split("/")[3])))
                elif path.startswith("/api/snapshots/"):
                    parts = path.split("/")
                    snapshot_id = int(parts[3])
                    if len(parts) == 5 and parts[4] == "csv":
                        self.reply(
                            200,
                            store.snapshot(snapshot_id, raw=True),
                            "text/csv; charset=utf-8",
                            f"yahoo-snapshot-{snapshot_id}.csv",
                        )
                    elif len(parts) == 4:
                        self.reply(200, store.snapshot(snapshot_id))
                    else:
                        self.reply(404, {"error": "Not found."})
                else:
                    self.reply(404, {"error": "Not found."})
            except ValueError as exc:
                self.reply(400, {"error": str(exc)})
            except KeyError:
                self.reply(404, {"error": "Research record not found."})
            except Exception:
                self.reply(500, {"error": "Could not read local data."})

        def do_POST(self):
            if not self.trusted(mutation=True):
                return
            try:
                self.connection.settimeout(15)
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > MAX_FILE_BYTES:
                    raise ValueError("Send a nonempty request no larger than 10 MB.")
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError("Incomplete request.")
                path = urlsplit(self.path).path
                if path.startswith("/api/sources/") and path.endswith("/import"):
                    source_id = int(path.split("/")[3])
                    self.reply(200, store.ingest(source_id, data))
                    return
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected application/json.")
                body = json.loads(data)
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object.")
                if research and path.startswith("/api/research/"):
                    self.research_post(path, body)
                elif path == "/api/companion/result":
                    self.reply(200, browser.complete(body))
                elif path.startswith("/api/sources/") and path.endswith("/selection"):
                    browser.select_source(int(path.split("/")[3]), body.get("selected"))
                    self.reply(200, {"saved": True})
                elif path == "/api/pull":
                    ids = [s["id"] for s in store.sources() if s["selected"] and s["url"]]
                    if not ids:
                        raise ValueError("Check at least one portfolio to pull.")
                    browser.submit("refresh", ids)
                    self.reply(202, {"accepted": True, "portfolio_count": len(ids)})
                elif path == "/api/sources":
                    for key in ("name", "account", "url", "currency"):
                        if body.get(key) is not None and not isinstance(body[key], str):
                            raise ValueError("Portfolio fields must be text.")
                    source_id = store.add_source(
                        body.get("name", ""),
                        body.get("account") or "",
                        body.get("url"),
                        body.get("currency"),
                    )
                    self.reply(201, {"id": source_id})
                elif path == "/api/browser":
                    ids = body.get("source_ids", [])
                    if not isinstance(ids, list) or any(type(i) is not int for i in ids):
                        raise ValueError("Portfolio IDs must be a list of integers.")
                    browser.submit(body.get("action"), ids)
                    self.reply(202, {"accepted": True})
                else:
                    self.reply(404, {"error": "Not found."})
            except (ValueError, UnicodeDecodeError) as exc:
                self.reply(400, {"error": str(exc)})
            except KeyError:
                self.reply(404, {"error": "Research record not found."})
            except TimeoutError:
                self.reply(408, {"error": "Request timed out."})
            except Exception:
                self.reply(500, {"error": "Operation failed. Previous snapshots are preserved."})

        def research_get(self, path):
            if path == "/api/research":
                self.reply(200, research.status())
            elif path == "/api/research/supplemental":
                self.reply(200, research.supplemental())
            elif path == "/api/research/current":
                self.reply(200, research.current())
            elif path == "/api/research/exceptions":
                self.reply(200, research.exceptions())
            elif path == "/api/research/workflows":
                self.reply(200, research.workflows())
            elif path.startswith("/api/research/workflows/"):
                self.reply(200, research.workflow(path.rsplit("/", 1)[-1]))
            elif path == "/api/research/providers/health":
                self.reply(200, research.provider_health())
            elif path.startswith("/api/research/runs/"):
                self.reply(200, research.run(path.rsplit("/", 1)[-1]))
            elif path.startswith("/api/research/jobs/"):
                self.reply(200, research.job(path.rsplit("/", 1)[-1]))
            elif path == "/api/research/records":
                query = parse_qs(urlsplit(self.path).query)
                kind = query.get("kind", ["decision"])[0]
                if kind not in {"decision", "evaluation", "assessment", "comparator"}:
                    raise ValueError("Choose a review record type.")
                from portfolio_research.public import _clean

                self.reply(
                    200, _clean(research.store.records(kind, query.get("run_id", [None])[0]))
                )
            elif path == "/api/research/compare":
                query = parse_qs(urlsplit(self.path).query)
                self.reply(
                    200, research.compare(query.get("left", [""])[0], query.get("right", [""])[0])
                )
            elif path.startswith("/api/research/export/"):
                from portfolio_lab.pipeline import report_html

                name = path.rsplit("/", 1)[-1]
                run_id, extension = name.rsplit(".", 1)
                result = research.run(run_id)["result"]
                if extension == "json":
                    self.reply(200, result, attachment=f"portfolio-review-{run_id}.json")
                elif extension == "html":
                    self.reply(
                        200,
                        report_html(result).encode(),
                        "text/html; charset=utf-8",
                        f"portfolio-review-{run_id}.html",
                    )
                else:
                    raise ValueError("Exports support JSON or HTML.")
            else:
                self.reply(404, {"error": "Not found."})

        def research_post(self, path, body):
            if path == "/api/research/jobs":
                self.reply(202, research.submit(body))
            elif path == "/api/research/workflows":
                self.reply(202, research.start_workflow(body))
            elif path.startswith("/api/research/workflows/") and path.endswith("/cancel"):
                self.reply(200, research.cancel_workflow(path.split("/")[4]))
            elif path == "/api/research/settings":
                self.reply(200, research.save_settings(body.get("patch", {})))
            elif path == "/api/research/inputs":
                self.reply(200, research.import_input(body))
            elif path == "/api/research/decision":
                self.reply(201, research.save_decision(body))
            elif path == "/api/research/resolutions":
                self.reply(201, research.save_resolution(body))
            elif path == "/api/research/valuation-link":
                self.reply(200, research.valuation_link(body))
            elif path == "/api/research/providers":
                self.reply(200, research.save_providers(body))
            else:
                self.reply(404, {"error": "Not found."})

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Portfolio Review: local Yahoo collection and research"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get(
            "PORTFOLIO_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")
        ),
    )
    parser.add_argument("--research-config", help="Optional private research configuration JSON")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    os.umask(0o077)
    directory = Path(args.data_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Avoid competing browser instances and source refreshes in one data directory.
    import fcntl

    lock = (directory / ".app.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Another app is already using this data directory.")
    store = Store(directory)
    browser = ChromeCompanion(store)
    from portfolio_lab.config import load_config, write_config
    from portfolio_research.service import ResearchService, default_config

    default_path = directory / "research-config.json"
    config_path = args.research_config or (str(default_path) if default_path.is_file() else None)
    config = load_config(config_path) if config_path else default_config(directory)
    if config_path is None:
        # The settings a first run resolved are written down where every documented
        # recovery command looks for them. Backup, restore, export and migrate all default
        # --config to this file, so a data directory the app made and never wrote to could
        # not be backed up at all — discovered only at the moment a backup was needed.
        write_config(default_path, config)
    research = ResearchService(config, companion=browser, recover=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(store, browser, args.port, research)
    )
    server.daemon_threads = True
    print(f"Portfolio Review: http://127.0.0.1:{args.port}", flush=True)
    print(f"Local data: {directory}", flush=True)
    print("Press Ctrl+C to stop. Yahoo stays signed in in your normal Chrome browser.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        research.close()
        lock.close()


if __name__ == "__main__":
    main()
