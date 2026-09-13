"""Compatibility import; serves the single Portfolio Review application."""

from portfolio_research.server import make_server


def serve(config, as_of=None, port=8765):
    server = make_server(config, port=port)
    print(f"Portfolio Review: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
