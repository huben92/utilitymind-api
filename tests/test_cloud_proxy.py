"""Verify the cloud gateway keeps simulator controls behind dashboard auth."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/api/auth/me":
            user = {"role": "admin"} if self.headers.get("Cookie") == "session=valid" else None
            body = json.dumps({"user": user, "csrf_token": "known-token"}).encode()
        elif self.path == "/api/health":
            body = b'{"ok":true}'
        else:
            body = b"simulator page"
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path.startswith("/api/") else "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class CloudProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.simulator = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        for server in (cls.api, cls.simulator):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.port = free_port()
        env = os.environ.copy()
        env.update(PORT=str(cls.port),
                   PRIVATE_API_URL=f"http://127.0.0.1:{cls.api.server_port}",
                   PRIVATE_SIMULATOR_URL=f"http://127.0.0.1:{cls.simulator.server_port}")
        cls.proxy = subprocess.Popen([sys.executable, str(ROOT / "cloud_proxy.py")], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            try:
                urlopen(f"http://127.0.0.1:{cls.port}/healthz", timeout=1).close()
                break
            except URLError:
                time.sleep(.1)
        else:
            raise RuntimeError("Proxy did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proxy.terminate()
        cls.proxy.wait(timeout=5)
        cls.api.shutdown()
        cls.simulator.shutdown()

    def request(self, path, method="GET", cookie="", token="", body=None):
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["X-CSRF-Token"] = token
        request = Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                          headers=headers, method=method)
        try:
            return urlopen(request, timeout=2)
        except HTTPError as error:
            return error

    def test_api_proxy_and_simulator_authorization(self):
        with self.request("/api/health") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"ok": True})
        with self.request("/simulator/") as response:
            self.assertEqual(response.status, 403)
        with self.request("/simulator/", cookie="session=valid") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"simulator page")
        with self.request("/simulator/api/sensors/x", method="POST", cookie="session=valid",
                          body=b'{"value":1}') as response:
            self.assertEqual(response.status, 403)
        with self.request("/simulator/api/sensors/x", method="POST", cookie="session=valid",
                          token="known-token", body=b'{"value":1}') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'{"value":1}')


if __name__ == "__main__":
    unittest.main()
