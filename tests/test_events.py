"""Exercise SSE over real HTTP sockets, including revocation and backpressure."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gateway.auth import Auth
from gateway.server import Server


def read_event(response):
    fields = {}
    while True:
        line = response.readline()
        if not line:
            raise EOFError("Event stream closed")
        line = line.decode().rstrip("\r\n")
        if not line:
            if "data" in fields:
                return fields.get("event", "message"), json.loads(fields["data"]), fields.get("id")
            fields = {}
        elif not line.startswith(":"):
            key, _, value = line.partition(":")
            fields[key] = value.lstrip(" ")


class SnapshotStore:
    def __init__(self, data):
        self.data = data
        self.calls = 0
        self.fail = False

    def status(self):
        return {"healthy": True}

    def dashboard(self):
        self.calls += 1
        if self.fail:
            raise ValueError("test snapshot failure")
        return {"nginx": self.status(), "summary": {"requests": self.calls},
                "configuration": {}, "label": "Данные\nсервера"}


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-events-")
        self.store = SnapshotStore(Path(self.temp.name))
        with patch.dict(os.environ, {"GATEWAY_ADMIN_PASSWORD": "test-password-12345"}):
            self.auth = Auth(self.store.data)
        self.token, self.session = self.auth.login("test-password-12345", "test")
        self.server = Server(("127.0.0.1", 0), self.store, self.auth)
        self.server.events.interval = .1
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connections = []
        self.responses = []

    def tearDown(self):
        for response in self.responses:
            response.close()
        for connection in self.connections:
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def connect(self, path="/api/events", authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.connections.append(connection)
        headers = {"Cookie": "gateway_session=" + self.token} if authenticated else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        self.responses.append(response)
        return response

    def test_authentication_and_stream_framing(self):
        self.assertEqual(self.connect(authenticated=False).status, 401)
        response = self.connect()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        self.assertEqual(response.getheader("X-Accel-Buffering"), "no")
        self.assertIsNone(response.getheader("Content-Length"))
        name, first, sequence = read_event(response)
        self.assertEqual(name, "dashboard")
        self.assertEqual(first["label"], "Данные\nсервера")
        _, second, next_sequence = read_event(response)
        self.assertGreater(int(next_sequence), int(sequence))
        self.assertGreater(second["summary"]["requests"], first["summary"]["requests"])

    def test_logout_closes_stream_and_reconnect_requires_login(self):
        response = self.connect()
        read_event(response)
        self.auth.logout(self.token)
        for _ in range(10):
            name, _, _ = read_event(response)
            if name == "auth_required":
                break
        self.assertEqual(name, "auth_required")
        self.assertEqual(response.read(), b"")
        self.assertEqual(self.connect().status, 401)

    def test_expired_session_closes_stream(self):
        response = self.connect()
        read_event(response)
        with self.auth.lock:
            self.auth.sessions[self.token]["expires"] = 0
        for _ in range(10):
            name, _, _ = read_event(response)
            if name == "auth_required":
                break
        self.assertEqual(name, "auth_required")

    def test_subscribers_share_snapshots_and_leave_request_capacity(self):
        self.server.events.capacity = 2
        self.server.events.interval = 1
        first = self.connect()
        one = read_event(first)
        second = self.connect()
        two = read_event(second)
        self.assertEqual(one, two)
        self.assertEqual(self.store.calls, 1)
        excess = self.connect()
        self.assertEqual(excess.status, 503)
        self.assertEqual(self.connect('/healthz', authenticated=False).status, 200)
        self.assertEqual(self.connect('/api/session').status, 200)
        first.close()
        second.close()
        deadline = time.monotonic() + 3
        while self.server.events.clients and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.server.events.clients, 0)
        self.assertEqual(self.connect().status, 200)

    def test_sampling_error_recovers_without_reconnecting(self):
        self.store.fail = True
        response = self.connect()
        self.assertEqual(read_event(response)[0], "stream_error")
        self.store.fail = False
        for _ in range(10):
            name, _, _ = read_event(response)
            if name == "dashboard":
                break
        self.assertEqual(name, "dashboard")

    def test_shutdown_releases_open_streams(self):
        response = self.connect()
        read_event(response)
        started = time.monotonic()
        self.server.shutdown()
        response.read()
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
