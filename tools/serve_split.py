"""Tiny CORS-enabled static server for the split_output/ directory.

Serves the generated blueprint pieces on a separate port so the webUI
(page origin http://127.0.0.1:8000) can fetch them cross-origin for
inspection without going through the API.
"""
import argparse
import functools
import http.server
import os


def build_handler(root: str) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=root, **kwargs)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.end_headers()

        def log_message(self, fmt, *args):
            print(f"[serve_split] {self.address_string()} - {fmt % args}")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="CORS static server for split_output/")
    parser.add_argument("--root", default="split_output", help="directory to serve")
    parser.add_argument("--port", type=int, default=8010, help="port to listen on")
    args = parser.parse_args()
    root = os.path.abspath(args.root)
    print(f"Serving {root} on http://127.0.0.1:{args.port} (CORS *)")
    http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), build_handler(root)
    ).serve_forever()


if __name__ == "__main__":
    main()
