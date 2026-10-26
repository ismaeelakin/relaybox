from http.client import HTTPConnection
from pathlib import Path
import json
import tempfile
from threading import Thread
import unittest

from relaybox.server import make_server
from relaybox.store import Store


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(Store(Path(self.temp.name) / "test.db"), 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, data=None, headers=None):
        client = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            client.request(method, path, None if data is None else json.dumps(data),
                headers or {"Content-Type": "application/json"})
            response = client.getresponse()
            body = response.read()
            return response.status, json.loads(body) if response.getheader("Content-Type").startswith("application/json") else body
        finally:
            client.close()

    def test_round_trip(self):
        status, created = self.request("POST", "/api/jobs", {"payload": {"task": "demo"}})
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/claim", {})
        self.assertEqual(status, 200)
        job = result["job"]
        self.assertEqual(job["id"], created["job"]["id"])
        self.assertEqual(self.request("POST", f"/api/jobs/{job['id']}/ack", {"lease_token": "wrong"})[0], 409)
        self.assertEqual(self.request("POST", f"/api/jobs/{job['id']}/ack", {"lease_token": job["lease_token"]})[0], 200)
        self.assertEqual(self.request("GET", "/api/jobs")[1]["counts"]["done"], 1)

    def test_validation_and_cross_origin_rejection(self):
        self.assertEqual(self.request("POST", "/api/jobs", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", [], {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/claim", {}, {"Content-Type": "application/json", "Origin": "https://example.test"})[0], 403)
        self.assertEqual(self.request("GET", "/health", headers={"Host": "evil.test"})[0], 403)

    def test_static_allowlist_and_health(self):
        self.assertEqual(self.request("GET", "/health")[1], {"status": "ok"})
        self.assertIn(b"RelayBox", self.request("GET", "/")[1])
        self.assertEqual(self.request("GET", "/../relaybox.db")[0], 404)
        self.assertEqual(self.request("GET", "/app.js")[0], 200)


if __name__ == "__main__":
    unittest.main()
