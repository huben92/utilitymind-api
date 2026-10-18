"""Same-origin Firebase Hosting gateway to the private pilot VM.

Cloud Run has no durable storage here. PostgreSQL, ClickHouse, MQTT ingestion,
and the simulator stay on the VM. Only the VM's private IP is reachable from
this service through Direct VPC egress.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = os.environ["PRIVATE_API_URL"].rstrip("/")
SIMULATOR_BASE = os.environ["PRIVATE_SIMULATOR_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 1_000_000
COPY_HEADERS = ("Cookie", "Content-Type", "X-CSRF-Token", "Authorization")
RESPONSE_HEADERS = ("Content-Type", "Content-Disposition", "Set-Cookie")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.forward()

    def do_HEAD(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def do_PUT(self):
        self.forward()

    def do_PATCH(self):
        self.forward()

    def do_DELETE(self):
        self.forward()

    def do_OPTIONS(self):
        self.forward()

    def respond(self, status, body, content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def fetch(self, url, body=None, method=None):
        headers = {name: self.headers[name] for name in COPY_HEADERS if self.headers.get(name)}
        request = Request(url, data=body, headers=headers, method=method or self.command)
        try:
            return urlopen(request, timeout=55)
        except HTTPError as error:
            return error

    def forward(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.respond(200, b"ok")
            return
        simulator = path == "/simulator" or path.startswith("/simulator/")
        if not simulator and not path.startswith("/api/"):
            self.respond(404, b"Not found")
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            self.respond(413, b"Request too large")
            return
        body = self.rfile.read(length) if length else None
        try:
            if simulator:
                with self.fetch(f"{API_BASE}/api/auth/me", method="GET") as auth_response:
                    identity = json.load(auth_response)
                user = identity.get("user") or {}
                if user.get("role") not in ("super_admin", "admin"):
                    self.respond(403, b"Admin login required")
                    return
                if self.command not in ("GET", "HEAD", "OPTIONS"):
                    token = self.headers.get("X-CSRF-Token", "")
                    if not token or token != identity.get("csrf_token"):
                        self.respond(403, b"Invalid session token")
                        return
                suffix = self.path[len("/simulator"):]
                target = SIMULATOR_BASE + (suffix or "/")
            else:
                target = API_BASE + self.path
            with self.fetch(target, body=body) as upstream:
                content = upstream.read(10_000_001)
                if len(content) > 10_000_000:
                    self.respond(502, b"Upstream response too large")
                    return
                self.send_response(upstream.status)
                for name in RESPONSE_HEADERS:
                    for value in upstream.headers.get_all(name, []):
                        self.send_header(name, value)
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(content)
        except (OSError, URLError, ValueError) as error:
            print(f"Gateway error: {error}", flush=True)
            self.respond(502, b"Private API unavailable")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
