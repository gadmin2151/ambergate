"""Control-plane availability regressions, with disposable data and loopback sockets."""
import copy
from email.message import Message
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ambergate.auth import Auth, LoginLimited
from ambergate.http_security import TrustedProxies
from ambergate.server import Server, Handler
from ambergate.storage import write_json
from ambergate.storage import Store
from ambergate.tunnel import connect
from ambergate.tunnel_hub import TunnelHub
from ambergate.agents import Agents
from ambergate.labels import reconcile, LabelController
from .helpers import config
from .test_agents import report
from .test_events import SnapshotStore, read_event


class ControlHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name))
        with patch.dict(os.environ, {"AMBERGATE_ADMIN_PASSWORD": "security-test-password"}):
            self.auth = Auth(self.store.data)
        self.server = Server(("127.0.0.1", 0), self.store, self.auth)
        self.server.events.interval = .05
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/healthz", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request("POST" if body else "GET", path, body, headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_dribbled_request_line_headers_and_body_have_absolute_deadlines(self):
        with patch.object(Handler, "header_timeout", .25), patch.object(Handler, "body_timeout", .25):
            for prefix in (b"G", b"GET /healthz HTTP/1.1\r\nHost: test\r\nX-Slow: ",
                           b"POST /api/login HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\nContent-Length: 10000\r\n\r\n"):
                with self.subTest(prefix=prefix[:20]):
                    conn = socket.create_connection(self.server.server_address, timeout=1)
                    try:
                        conn.sendall(prefix)
                        start = time.monotonic()
                        while time.monotonic() - start < .55:
                            try:
                                conn.sendall(b"x")
                            except OSError:
                                break
                            time.sleep(.04)
                        self.assertEqual(conn.recv(1), b"")
                    finally:
                        conn.close()
                    self.assertEqual(self.request()[0], 200)

    def test_one_source_cannot_hold_all_pending_slots(self):
        held = []
        try:
            for _ in range(20):
                conn = socket.socket()
                conn.bind(("127.0.0.2", 0))
                conn.connect(self.server.server_address)
                held.append(conn)
            time.sleep(.08)
            self.assertLessEqual(self.server.admission.counts["pending"], 8)
            self.assertEqual(self.request()[0], 200)
        finally:
            for conn in held:
                conn.close()

    def test_streaming_proxy_cannot_share_all_body_slots_with_one_client(self):
        proxies = TrustedProxies("127.0.0.1/32")
        self.server.trusted_proxies = proxies
        self.server.admission.proxies = proxies
        held = []
        try:
            for _ in range(64):
                conn = socket.create_connection(self.server.server_address, timeout=2)
                held.append(conn)
                conn.sendall(b"POST /api/login HTTP/1.1\r\nHost: test\r\nX-Forwarded-For: 198.51.100.1\r\nContent-Type: application/json\r\nContent-Length: 10000\r\n\r\nx")
            time.sleep(.1)
            self.assertLessEqual(self.server.admission.counts["pending"], 8)
            token, _ = self.auth.login("security-test-password", "fixture")
            self.assertEqual(self.request("/api/session", headers={"Cookie": "ambergate_session=" + token,
                             "X-Forwarded-For": "198.51.100.2"})[0], 200)
        finally:
            for conn in held:
                conn.close()

    def test_authenticated_sse_and_agent_control_survive_header_deadline(self):
        token, _ = self.auth.login("security-test-password", "fixture")
        agent = self.server.agents.mutate(dict(action="create", name="test", revision=self.server.agents.revision()))
        with patch.object(Handler, "header_timeout", .15):
            conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
            conn.request("GET", "/api/events", headers={"Cookie": "ambergate_session=" + token})
            response = conn.getresponse()
            ws = connect(f"http://127.0.0.1:{self.server.server_port}", "/api/agents/connect", agent["token"], allow_http=True)
            try:
                self.assertEqual(response.status, 200)
                read_event(response)
                time.sleep(.35)
                self.assertEqual(read_event(response)[0], "dashboard")
                ws.send(b"still-live", 9)
                # Server must consume the ping after its original header deadline.
                time.sleep(.1)
                self.assertTrue(self.server.tunnels.connected(agent["agent_id"]))
                self.assertEqual(self.request()[0], 200)
            finally:
                ws.close()
                self.auth.logout(token)
                response.close()
                conn.close()

    def test_trusted_proxy_clients_get_independent_login_budgets(self):
        self.server.trusted_proxies = TrustedProxies("127.0.0.1/32")
        headers = {"Content-Type": "application/json", "X-Forwarded-For": "198.51.100.1"}
        for _ in range(10):
            self.assertEqual(self.request("/api/login", '{"password":"wrong"}', headers)[0], 401)
        status, retry, _ = self.request("/api/login", '{"password":"wrong"}', headers)
        self.assertEqual(status, 429)
        self.assertLessEqual(int(retry["Retry-After"]), 30)
        status, _, _ = self.request("/api/login", '{"password":"security-test-password"}',
                                    {**headers, "X-Forwarded-For": "198.51.100.2"})
        self.assertEqual(status, 200)

    def test_untrusted_forwarded_ip_does_not_reset_login_budget(self):
        headers = {"Content-Type": "application/json"}
        for n in range(10):
            self.assertEqual(self.request("/api/login", '{"password":"wrong"}',
                             {**headers, "X-Forwarded-For": f"198.51.100.{n}"})[0], 401)
        self.assertEqual(self.request("/api/login", '{"password":"wrong"}',
                         {**headers, "X-Forwarded-For": "198.51.100.250"})[0], 429)


class IdentityTests(unittest.TestCase):
    def test_proxy_chain_parsing_does_not_trust_attacker_prepended_ip(self):
        proxies = TrustedProxies("127.0.0.1/32, 10.0.0.2/32")
        for value, expected in (("192.0.2.9, 198.51.100.4, 10.0.0.2", "198.51.100.4"),
                                ("bad, 198.51.100.4", "127.0.0.1"),
                                ("::ffff:198.51.100.4", "198.51.100.4"),
                                ("198.51.100.4%zone", "127.0.0.1")):
            headers = Message(); headers["X-Forwarded-For"] = value
            self.assertEqual(proxies.client("127.0.0.1", headers), expected)
            self.assertEqual(proxies.client("203.0.113.1", headers), "203.0.113.1")
        headers["X-Forwarded-For"] = "192.0.2.1"
        self.assertEqual(proxies.client("127.0.0.1", headers), "127.0.0.1")
        with self.assertRaises(ValueError):
            TrustedProxies("0.0.0.0/0")

    def test_password_work_is_bounded_without_blocking_existing_sessions(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AMBERGATE_ADMIN_PASSWORD": "security-test-password"}):
            auth = Auth(Path(temp))
            token, _ = auth.login("security-test-password", "fixture")
            entered, release = threading.Event(), threading.Event()
            def slow(*args):
                entered.set(); release.wait(3)
                return "invalid"
            with patch("ambergate.auth.hash_password", side_effect=slow):
                threads = [threading.Thread(target=auth.login, args=("wrong", str(n))) for n in range(4)]
                try:
                    for thread in threads: thread.start()
                    self.assertTrue(entered.wait(1))
                    deadline = time.monotonic() + 1
                    while len(auth.login_sources) < 4 and time.monotonic() < deadline: time.sleep(.01)
                    with self.assertRaises(LoginLimited): auth.login("wrong", "fifth")
                    self.assertIsNotNone(auth.session(token))
                finally:
                    release.set()
                    for thread in threads: thread.join(2)


class AgentAllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.hub = TunnelHub(self.root); self.addCleanup(self.hub.stop)
        self.now = 1000
        self.agents = Agents(self.root, self.hub, clock=lambda: self.now)

    def create(self):
        agent = self.agents.mutate(dict(action="create", name="test", revision=self.agents.revision()))
        self.hub.attach(agent["agent_id"], Mock(closed=threading.Event()))
        return agent["agent_id"], "Bearer " + agent["token"]

    def apply(self):
        candidate = reconcile(config(), self.agents.routing()[0])
        with self.agents.materialize(candidate) as (applied,):
            return applied

    def test_inventory_and_review_churn_do_not_allocate(self):
        aid, token = self.create()
        for n in range(150):
            self.now += 2
            self.assertTrue(self.agents.report(token, report(name=f"app-{n}"))["ok"])
            reconcile(config(), self.agents.routing()[0])
        self.assertEqual(self.hub.ports, {})
        self.assertFalse(self.hub.file.exists())
        applied = self.apply()
        self.assertEqual(len(self.hub.ports), 1)
        self.assertNotIn("pending-", json.dumps(applied))
        self.now += 2
        self.agents.report(token, report(identity="b", name="app-149"))
        # No scan/apply is needed to follow recreation while label automation is Off.
        from ambergate.agents import target_id
        self.assertEqual(list(self.hub.destinations[aid].values()), [target_id("b" * 64, 8080)])

    def test_report_quota_preserves_manual_targets_and_other_agents(self):
        self.hub.agent_capacity = 2
        aid, token = self.create(); other, other_token = self.create()
        raw = report(); raw["containers"][0]["route_labels"] = {}
        self.agents.report(token, raw, manual_routing=True)
        self.agents.select_targets(dict(agent_id=aid, targets=[dict(container_id="a" * 64, port=9000)]))
        self.now += 2
        raw = report()
        raw["containers"][0]["id"] = "b" * 64
        raw["containers"][0]["route_labels"] = {f"ambergate.route.r{n}": f"host=remote.test;path=/r{n};port={8000+n}" for n in range(3)}
        result = self.agents.report(token, raw, manual_routing=True)
        self.assertFalse(result["ok"])
        self.assertIn("quota", result["error"])
        self.assertEqual(len(self.hub.ports), 1)
        self.assertIn("docker:" + "b" * 64 + ":9000", self.hub.destinations[aid].values())
        self.assertTrue(self.agents.report(other_token, report())["ok"])
        self.apply()
        self.assertEqual(len(self.hub.ports), 2)

    def test_reservations_are_atomic_and_existing_over_quota_ports_survive(self):
        aid, _ = self.create()
        self.hub.agent_capacity = 2
        with self.assertRaisesRegex(ValueError, "quota"):
            with self.hub.reserve([(aid, "app", p) for p in (80, 81, 82)]): pass
        self.assertEqual(self.hub.ports, {})
        with self.assertRaisesRegex(RuntimeError, "apply failed"):
            with self.hub.reserve([(aid, "app", 80)]): raise RuntimeError("apply failed")
        self.assertEqual(self.hub.ports, {})
        _, first = self.hub.endpoint(aid, "app", 80)
        self.hub.endpoint(aid, "app", 81)
        self.hub.stop()
        with patch.dict(os.environ, {"AMBERGATE_AGENT_TARGET_LIMIT": "1"}):
            self.hub = TunnelHub(self.root)
        self.addCleanup(self.hub.stop)
        self.assertEqual(self.hub.endpoint(aid, "app", 80)[1], first)
        with self.assertRaisesRegex(ValueError, "quota"): self.hub.endpoint(aid, "new", 80)
        self.agents.hub = self.hub
        other, _ = self.create()
        self.hub.endpoint(other, "other", 80)

    def test_startup_prunes_only_unreferenced_reservations(self):
        aid, _ = self.create()
        _, active = self.hub.endpoint(aid, "active", 80)
        _, history = self.hub.endpoint(aid, "history", 80)
        _, manual = self.hub.endpoint(aid, "manual", 80)
        self.hub.endpoint(aid, "orphan", 80)
        active_file, history_file = self.root / "draft.json", self.root / "old.json"
        write_json(active_file, {"target": {"address": "127.0.0.1", "port": active}})
        write_json(history_file, {"target": {"address": "127.0.0.1", "port": history}})
        write_json(self.agents.manual_file, [dict(id=aid, container="manual", port=80)])
        self.hub.stop()
        restored = TunnelHub(self.root, config_paths=[active_file, history_file])
        self.addCleanup(restored.stop)
        self.assertEqual(set(restored.ports.values()), {active, history, manual})

    def test_quota_error_keeps_existing_label_destinations_current(self):
        from ambergate.agents import target_id
        self.hub.agent_capacity = 1
        aid, token = self.create()
        self.agents.report(token, report())
        self.apply()
        raw = report(identity="b")
        raw["containers"] += report(identity="c", name="new")["containers"]
        for _ in range(8):
            self.now += 5
            self.assertFalse(self.agents.report(token, raw)["ok"])
            self.assertEqual(list(self.hub.destinations[aid].values()), [target_id("b" * 64, 8080)])
            self.assertEqual(len(self.agents.routing()[0]), 1)

    def test_pruned_legacy_preview_is_materialized_with_a_reserved_port(self):
        aid, token = self.create()
        self.agents.report(token, report())
        self.apply()
        old = self.agents.reports[aid]
        legacy = {**old, "entries": self.agents.routing()[0], "destinations": self.hub.destinations[aid]}
        legacy.pop("routes")
        write_json(self.agents.directory / (aid + ".json"), legacy)
        self.hub.stop()
        self.hub = TunnelHub(self.root, config_paths=[])
        self.addCleanup(self.hub.stop)
        self.assertEqual(self.hub.ports, {})
        self.agents = Agents(self.root, self.hub, clock=lambda: self.now)
        candidate = reconcile(config(), self.agents.routing()[0])
        with self.agents.materialize(candidate) as (applied,):
            target = applied["hosts"][-1]["routes"][0]["docker"]["targets"][0]
            self.assertIn(target["port"], self.hub.ports.values())
        self.assertEqual(len(self.hub.ports), 1)

    def test_post_reload_metadata_error_preserves_committed_tunnel(self):
        aid, token = self.create()
        self.agents.report(token, report())
        with patch.object(Store, "check", return_value="ok"):
            store = Store(self.root, self.root / "cache", self.root / "run")
        self.addCleanup(store.metrics.stop)
        store.process = Mock()
        store.process.poll.return_value = None
        docker = Mock()
        docker.snapshot.return_value = dict(connected=False, truncated=False, settings={"enabled": False})
        labels = LabelController(store, docker, self.agents)
        labels.save("preview", labels.snapshot()["revision"])
        with patch.object(store, "check", return_value="ok"), patch.object(store, "wait_generation", return_value=True), \
                patch.object(store, "mark_applied", side_effect=OSError("metadata unavailable")):
            outcome = labels.scan(apply=True)
        self.assertTrue(outcome["errors"])
        target = store.active_config()["hosts"][-1]["routes"][0]["docker"]["targets"][0]
        self.assertIn(target["port"], self.hub.ports.values())
        self.assertIn(target["port"], json.loads(self.hub.file.read_text()).values())


if __name__ == "__main__":
    unittest.main()
