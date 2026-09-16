"""Factory for the unified app server, also used by isolated HTTP integration tests."""

from http.server import ThreadingHTTPServer
from pathlib import Path

from portfolio.companion import ChromeCompanion
from portfolio.server import make_handler
from portfolio.storage import Store

from .service import ResearchService


def make_server(config, port=8765, collector_directory=None):
    directory = Path(collector_directory or Path(config["research"]["path"]).parent / "collector")
    store = Store(directory)
    companion = ChromeCompanion(store)
    # Recovery on every serving entry point: an operation whose owning process is gone
    # would otherwise stay active and block every later review.
    research = ResearchService(config, companion=companion, recover=True)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            super().server_close()
            research.close()

    server = Server(("127.0.0.1", port), make_handler(store, companion, port, research))
    server.research = research
    return server
