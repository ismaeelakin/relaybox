"""Loopback-only reference HTTP adapter; not an internet-facing server."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .store import Conflict, NotFound, Store


def make_server(store, port=8080):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, fmt, *args):
            pass  # Do not log payloads, tokens or URL query strings.

        def send(self, status, data, content_type="application/json; charset=utf-8"):
            body = data if isinstance(data, bytes) else json.dumps(data, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"
            }

        def do_GET(self):
            if not self.host_ok():
                self.send(403, {"error": "untrusted Host"})
                return
            url = urlsplit(self.path)
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if url.path in assets:
                name, mime = assets[url.path]
                self.send(200, (Path(__file__).parent / "static" / name).read_bytes(), mime)
                return
            if url.path == "/health":
                self.send(200, {"status": "ok"})
                return
            if url.path == "/api/jobs":
                try:
                    queue = parse_qs(url.query).get("queue", ["default"])[0]
                    self.send(200, store.snapshot(queue))
                except ValueError as exc:
                    self.send(400, {"error": str(exc)})
                return
            self.send(404, {"error": "route not found"})

        def do_POST(self):
            if not self.host_ok():
                self.send(403, {"error": "untrusted Host"})
                return
            origin = self.headers.get("Origin")
            expected = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if origin is not None and origin not in expected:
                self.send(403, {"error": "cross-origin writes are disabled"})
                return
            if self.headers.get_content_type() != "application/json":
                self.send(415, {"error": "Content-Type must be application/json"})
                return
            if self.headers.get("Transfer-Encoding") is not None:
                self.send(400, {"error": "transfer encoding is unsupported; send Content-Length"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 70000:
                    self.send(413, {"error": "request body must be between 1 and 70000 bytes"})
                    return
                raw = self.rfile.read(length)
                data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
                if not isinstance(data, dict):
                    raise ValueError("body must be a JSON object")
                path = urlsplit(self.path).path
                if path == "/api/jobs":
                    if "payload" not in data:
                        raise ValueError("payload is required")
                    job, created = store.enqueue(data.get("queue", "default"), data["payload"],
                        data.get("idempotency_key"), data.get("delay", 0), data.get("max_attempts", 3))
                    self.send(201 if created else 200, {"job": job, "created": created})
                elif path == "/api/claim":
                    self.send(200, {"job": store.claim(data.get("queue", "default"), data.get("lease_seconds", 30))})
                elif match := re.fullmatch(r"/api/jobs/([a-f0-9-]{36})/(ack|fail|retry)", path):
                    job_id, action = match.groups()
                    if action == "ack":
                        store.ack(job_id, data.get("lease_token"))
                    elif action == "fail":
                        store.fail(job_id, data.get("lease_token"), data.get("error", "worker failed"))
                    else:
                        store.retry(job_id)
                    self.send(200, {"ok": True})
                else:
                    self.send(404, {"error": "route not found"})
            except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                self.send(400, {"error": str(exc)})
            except Conflict as exc:
                self.send(409, {"error": str(exc)})
            except NotFound as exc:
                self.send(404, {"error": str(exc)})
            except sqlite3.OperationalError:
                self.send(503, {"error": "storage temporarily unavailable; retry later"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="RelayBox local durable queue console")
    parser.add_argument("--db", default="relaybox.db")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = make_server(Store(args.db), args.port)
    print(f"RelayBox running at http://127.0.0.1:{server.server_port} (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
