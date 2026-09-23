"""Real nginx, real HTTP and real reloads. Set TEST_NGINX and TEST_MIME_TYPES."""
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

from ambergate.auth import Auth
from ambergate.server import Server
from ambergate.storage import ApplyError, ConflictError, Store, atomic_write
from .helpers import config, route
from .test_labels import snapshot as label_snapshot
from ambergate.labels import LabelController
from ambergate.nginx import digest

NGINX = os.environ.get("TEST_NGINX") or shutil.which("nginx")
MIME = os.environ.get("TEST_MIME_TYPES", "/etc/nginx/mime.types")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, path="/", method="GET", headers=None, body=None):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        client.request(method, path, body=body, headers={"Host": "gateway.test", **(headers or {})})
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.count += 1
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.close_connection = True
            return
        if self.path.startswith('/response-test'):
            from urllib.parse import urlparse, parse_qs
            args=parse_qs(urlparse(self.path).query)
            data=args.get('html',['<base href="/"><a href="/login">login</a>'])[0].encode()
            self.send_response(302 if 'to' in args else 200)
            if 'to' in args:self.send_header('Location',args['to'][0])
            self.send_header('Set-Cookie','session=x; Path=/; HttpOnly; SameSite=Lax')
            self.send_header('Content-Type','text/html')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data);return
        data = json.dumps({"server": self.server.server_port, "path": self.path,
                           "count": self.server.count, "headers": dict(self.headers)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if "set-cookie" in self.path:
            self.send_header("Set-Cookie", "session=private")
        if "private" in self.path:
            self.send_header("Cache-Control", "private, no-store")
        if "vary" in self.path:
            self.send_header("Vary", "Accept-Language")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.do_GET()

    def log_message(self, *_):
        pass


@unittest.skipUnless(NGINX and Path(MIME).is_file(), "Set TEST_NGINX and TEST_MIME_TYPES to run real nginx tests")
class GatewayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="gateway-test-")
        cls.root = Path(cls.temp.name)
        # In the Docker image the master is root and workers drop privileges.
        # Allow workers to traverse to their own cache and temporary directories.
        cls.root.chmod(0o755)
        cls.backends = []
        for _ in range(2):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            server.count = 0
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.backends.append(server)
        cls.port, cls.control = free_port(), free_port()
        cls.store = Store(cls.root / "data", cls.root / "cache", cls.root / "run", NGINX,
                          port=cls.port, control_port=cls.control, mime_types=MIME)
        cls.store.start()
        with patch.dict(os.environ, {"AMBERGATE_ADMIN_PASSWORD": "test-password-12345"}):
            cls.auth = Auth(cls.store.data)
        cls.admin = Server(("127.0.0.1", 0), cls.store, cls.auth)
        cls.admin_thread = threading.Thread(target=cls.admin.serve_forever, daemon=True)
        cls.admin_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.admin.shutdown()
        cls.admin.server_close()
        cls.store.stop()
        for backend in cls.backends:
            backend.shutdown()
            backend.server_close()
        cls.temp.cleanup()

    def setUp(self):
        self.config = config(self.backends[0].server_port)
        self.apply_config()

    def test_labels_preview_reload_draft_isolation_scale_to_zero_and_recovery(self):
        docker = Mock()
        docker.snapshot.return_value = label_snapshot(port=self.backends[1].server_port)
        labels = LabelController(self.store, docker)
        labels.save("preview", labels.snapshot()["revision"])
        try:
            before = self.store.snapshot()
            preview = labels.scan()
            self.assertFalse(preview["errors"])
            self.assertEqual(self.store.snapshot(), before)
            self.assertEqual(len(preview["changes"]), 1)
            # A saved unrelated edit must remain pending through every label reload.
            draft = copy.deepcopy(before["config"])
            draft["settings"]["body_mb"] = 123
            self.store.save(draft, before["revision"])
            with self.assertRaises(ConflictError):
                labels.scan(apply=True, token=preview["token"])
            result = labels.scan(apply=True, token=labels.scan()["token"])
            self.assertFalse(result["errors"])
            self.assertEqual(self.store.active_config()["settings"]["body_mb"], before["config"]["settings"]["body_mb"])
            self.assertEqual(self.store.draft_config()["settings"]["body_mb"], 123)
            self.assertTrue(self.store.snapshot()["pending"])
            status, _, body = request(self.port, "/api/users", headers={"Host": "example.com"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["server"], self.backends[1].server_port)
            generation = self.store.active_id()
            labels.scan(apply=True)
            self.assertEqual(self.store.active_id(), generation, "unchanged labels must not reload Nginx")
            docker.snapshot.return_value["containers"][0]["state"] = "exited"
            self.assertFalse(labels.scan(apply=True)["errors"])
            for path in ("/api", "/api/users"):
                self.assertEqual(request(self.port, path, headers={"Host": "example.com"})[0], 503)
            self.assertEqual(request(self.port, "/api2", headers={"Host": "example.com"})[0], 404)
            self.assertEqual(request(self.port)[0], 200)
            docker.snapshot.return_value["containers"][0]["state"] = "running"
            docker.snapshot.return_value["containers"][0]["route_labels"]["ambergate.route"] += ";backup=true"
            self.assertFalse(labels.scan(apply=True)["errors"])
            self.assertEqual(request(self.port, "/api", headers={"Host": "example.com"})[0], 200)
        finally:
            labels.file.unlink(missing_ok=True)

    def test_labels_failure_preserves_active_and_draft(self):
        docker = Mock()
        data = label_snapshot(port=self.backends[0].server_port)
        docker.snapshot.return_value = data
        labels = LabelController(self.store, docker)
        labels.save("preview", labels.snapshot()["revision"])
        try:
            before = self.store.snapshot()
            with patch.object(self.store, "check", side_effect=ApplyError("invalid generated nginx")):
                self.assertTrue(labels.scan(apply=True)["errors"])
            self.assertEqual(self.store.snapshot(), before)
            for key in ("connected", "truncated"):
                data[key] = key == "truncated"
                self.assertTrue(labels.scan(apply=True)["errors"])
                self.assertEqual(self.store.snapshot(), before)
                data[key] = key == "connected"
            data["containers"][0]["route_labels"]["ambergate.route"] += ";unknown=1"
            self.assertTrue(labels.scan(apply=True)["errors"])
            self.assertEqual(self.store.snapshot(), before)
        finally:
            labels.file.unlink(missing_ok=True)

    def apply_config(self):
        snapshot = self.store.snapshot()
        saved = self.store.save(self.config, snapshot["revision"])
        return self.store.apply(saved["revision"])

    def test_path_boundaries_strip_prefix_and_host(self):
        self.config["hosts"][0]["routes"].append(route("/api", self.backends[1].server_port, "api"))
        self.config["hosts"][0]["routes"][1]["strip_prefix"] = True
        self.apply_config()
        for incoming, outgoing, backend in [("/", "/", 0), ("/api", "/", 1),
            ("/api/users?q=1", "/users?q=1", 1), ("/apix", "/apix", 0)]:
            status, _, body = request(self.port, incoming)
            data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(data["path"], outgoing)
            self.assertEqual(data["server"], self.backends[backend].server_port)
            self.assertEqual(data["headers"]["Host"], "gateway.test")
        self.assertEqual(request(self.port, headers={"Host": "unknown.test"})[0], 404)

    def test_response_prefix_rewrites_redirects_cookies_html_and_custom_rules(self):
        from urllib.parse import urlencode
        api=route('/api5',self.backends[0].server_port,'response_prefix')
        api.update(strip_prefix=True,response_rewrite=[{'search':'"basePath":""','replace':'"basePath":"{prefix}"'}])
        self.config['hosts'][0]['routes'].append(api);self.apply_config()
        self.assertEqual(request(self.port,'/api5?x=1')[0],308)
        self.assertEqual(request(self.port,'/api5?x=1')[1]['Location'],'/api5/?x=1')
        for original, expected in [('/', '/api5/'),('/login?next=1','/api5/login?next=1'),('/api5/login','/api5/login'),('//other.test/login','//other.test/login'),('https://other.test/login','https://other.test/login'),('http://gateway.test/login','/api5/login')]:
            status,headers,_=request(self.port,'/api5/response-test?'+urlencode({'to':original}))
            self.assertEqual(status,302);self.assertEqual(headers['Location'],expected)
            self.assertIn('Path=/api5/',headers['Set-Cookie'])
        html='<base href="/"><a href="/login">a</a><img src="//cdn.test/a"><a href="/api5/login">b</a><a href="https://other.test">c</a><script>{"basePath":""}</script>'
        status,_,body=request(self.port,'/api5/response-test?'+urlencode({'html':html}))
        self.assertEqual(status,200)
        self.assertIn(b'<base href="/api5/">',body);self.assertIn(b'href="/api5/login"',body)
        self.assertIn(b'src="//cdn.test/a"',body);self.assertNotIn(b'/api5/api5/',body)
        self.assertIn(b'"basePath":"/api5"',body)
        self.assertEqual(request(self.port,'/response-test?'+urlencode({'to':'/login'}))[1]['Location'],'/login')

    def test_multiple_domains_have_independent_routes(self):
        first = self.config["hosts"][0]
        api = route("/api", self.backends[0].server_port, "first_api")
        api["strip_prefix"] = True
        first["routes"].append(api)
        second = {"id": "second_host", "domain": "second.test", "enabled": True,
                  "routes": [route("/", self.backends[1].server_port, "second_root"),
                             route("/api", self.backends[1].server_port, "second_api")]}
        self.config["hosts"].append(second)
        result = self.apply_config()
        self.assertEqual(len(result["config"]["hosts"]), 2)
        for domain, backend, api_path in [("gateway.test", 0, "/users"),
                                          ("second.test", 1, "/api/users")]:
            for path, expected in [("/", "/"), ("/api/users", api_path)]:
                status, _, body = request(self.port, path, headers={"Host": domain})
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertEqual(data["server"], self.backends[backend].server_port)
                self.assertEqual(data["path"], expected)
                self.assertEqual(data["headers"]["Host"], domain)
        first["enabled"] = False
        self.apply_config()
        self.assertEqual(request(self.port)[0], 404)
        self.assertEqual(request(self.port, headers={"Host": "second.test"})[0], 200)

    def test_balancing_cache_and_private_bypass(self):
        r = self.config["hosts"][0]["routes"][0]
        r["targets"].append({"address": "127.0.0.1", "port": self.backends[1].server_port, "weight": 1, "backup": False})
        self.apply_config()
        servers = {json.loads(request(self.port)[2])["server"] for _ in range(12)}
        self.assertEqual(servers, {s.server_port for s in self.backends})
        r["cache"] = True
        self.apply_config()
        first = request(self.port, "/cached")
        second = request(self.port, "/cached")
        self.assertEqual(first[1]["X-AmberGate-Cache"], "MISS")
        self.assertEqual(second[1]["X-AmberGate-Cache"], "HIT")
        self.assertEqual(first[2], second[2])
        for headers in ({"Cookie": "user=one"}, {"Authorization": "Bearer secret"},
                        {"Cache-Control": "no-cache"}):
            self.assertEqual(request(self.port, "/cached", headers=headers)[1]["X-AmberGate-Cache"], "BYPASS")
        for path in ("/signed?X-Amz-Signature=abc", "/signed?token=abc"):
            self.assertEqual(request(self.port, path)[1]["X-AmberGate-Cache"], "BYPASS")
        for path in ("/set-cookie", "/private"):
            self.assertEqual(request(self.port, path)[1]["X-AmberGate-Cache"], "MISS")
            self.assertEqual(request(self.port, path)[1]["X-AmberGate-Cache"], "MISS")
        self.assertNotEqual(request(self.port, "/post", method="POST", body="abc")[1].get("X-AmberGate-Cache"), "HIT")
        en = request(self.port, "/vary", headers={"Accept-Language": "en"})
        ru = request(self.port, "/vary", headers={"Accept-Language": "ru"})
        self.assertNotEqual(en[2], ru[2])

    def test_deleting_routes_persists_and_last_route_returns_404(self):
        host = self.config["hosts"][0]
        host["routes"].append(route("/api", self.backends[1].server_port, "api"))
        self.apply_config()
        # Removing the root route must keep the remaining /api destination.
        host["routes"] = [host["routes"][1]]
        saved = self.store.save(self.config, self.store.snapshot()["revision"])
        self.assertEqual(request(self.port)[0], 200)
        self.store.apply(saved["revision"])
        self.assertEqual(request(self.port)[0], 404)
        self.assertEqual(json.loads(request(self.port, "/api")[2])["server"],
                         self.backends[1].server_port)
        host["routes"] = []
        saved = self.store.save(self.config, self.store.snapshot()["revision"])
        self.assertEqual(saved["config"]["hosts"][0]["routes"], [])
        self.store.apply(saved["revision"])
        self.store.stop()
        self.store.start()
        self.assertEqual(self.store.draft_config()["hosts"][0]["routes"], [])
        self.assertEqual(self.store.active_config()["hosts"][0]["domain"], "gateway.test")
        self.assertEqual(request(self.port)[0], 404)
        self.assertEqual(request(self.port, "/api")[0], 404)

    def test_limits_trusted_ip_and_forwarded_headers(self):
        s = self.config["settings"]
        s.update(rate_rps=1, rate_burst=0, trusted_proxies=["127.0.0.1/32"])
        self.apply_config()
        headers = {"X-Forwarded-For": "198.51.100.10", "X-Forwarded-Proto": "https"}
        first = request(self.port, headers=headers)
        self.assertEqual(first[0], 200)
        forwarded = json.loads(first[2])["headers"]
        self.assertEqual(forwarded["X-Real-IP"], "198.51.100.10")
        self.assertEqual(forwarded["X-Forwarded-Proto"], "https")
        self.assertEqual(request(self.port, headers=headers)[0], 429)
        self.assertEqual(request(self.port, headers={"X-Forwarded-For": "198.51.100.11"})[0], 200)
        s.update(rate_rps=0, trusted_proxies=[])
        self.apply_config()
        forwarded = json.loads(request(self.port, headers=headers)[2])["headers"]
        self.assertEqual(forwarded["X-Real-IP"], "127.0.0.1")
        self.assertEqual(forwarded["X-Forwarded-For"], "127.0.0.1")
        self.assertEqual(forwarded["X-Forwarded-Proto"], "http")

    def test_route_rate_limit_and_websocket_upgrade(self):
        self.config["settings"].update(rate_rps=100, rate_burst=40)
        api = route("/api", self.backends[0].server_port, "api")
        api.update(rate_rps=1, rate_burst=0)
        self.config["hosts"][0]["routes"].append(api)
        self.apply_config()
        self.assertEqual(request(self.port, "/api")[0], 200)
        self.assertEqual(request(self.port, "/api/users")[0], 429)
        self.assertEqual(request(self.port, "/")[0], 200)
        result = request(self.port, "/ws", headers={"Connection": "Upgrade", "Upgrade": "websocket"})
        self.assertEqual(result[0], 101)
        self.assertEqual(result[1]["Upgrade"], "websocket")

    def test_reload_switches_new_connections_before_returning(self):
        # Exercise the brief overlap between new and retiring Nginx workers.
        # Fresh connections after apply must see both the destination and
        # trusted-proxy settings of the confirmed configuration.
        for change in range(6):
            backend = self.backends[change % 2]
            trusted = bool(change % 2)
            self.config["hosts"][0]["routes"][0]["targets"][0]["port"] = backend.server_port
            self.config["settings"]["trusted_proxies"] = ["127.0.0.1/32"] if trusted else []
            self.apply_config()
            for _ in range(4):
                status, _, body = request(self.port, headers={"X-Forwarded-For": "198.51.100.42"})
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertEqual(data["server"], backend.server_port)
                self.assertEqual(data["headers"]["X-Real-IP"],
                                 "198.51.100.42" if trusted else "127.0.0.1")

    def test_balance_methods_backup_failover_and_s3_path(self):
        r = self.config["hosts"][0]["routes"][0]
        r["targets"].append({"address": "127.0.0.1", "port": self.backends[1].server_port,
                             "weight": 1, "backup": False})
        for method in ("least_conn", "ip_hash"):
            r["balance"] = method
            self.apply_config()
            results = [json.loads(request(self.port)[2])["server"] for _ in range(8)]
            self.assertTrue(set(results).issubset({s.server_port for s in self.backends}))
            if method == "ip_hash":
                self.assertEqual(len(set(results)), 1)
        r["balance"] = "least_conn"
        r["targets"][0]["port"] = free_port()
        r["targets"][1]["backup"] = True
        self.apply_config()
        self.assertEqual(json.loads(request(self.port)[2])["server"], self.backends[1].server_port)
        s3 = route("/s3", self.backends[0].server_port, "s3")
        self.config["hosts"][0]["routes"].append(s3)
        self.apply_config()
        incoming = "/s3/bucket/a%20b?X-Amz-Signature=abc"
        result = request(self.port, incoming, headers={"Host": "gateway.test:80"})
        self.assertEqual(json.loads(result[2])["path"], incoming)
        self.assertEqual(json.loads(result[2])["headers"]["Host"], "gateway.test:80")

    def test_security_body_limit_and_missing_dns(self):
        self.assertEqual(request(self.port, "/.env")[0], 404)
        self.assertEqual(request(self.port, "/.git/config")[0], 404)
        self.assertEqual(request(self.port, "/.well-known/test")[0], 200)
        self.assertEqual(request(self.port, method="TRACE")[0], 405)
        self.assertEqual(request(self.port)[1]["X-Content-Type-Options"], "nosniff")
        self.config["hosts"][0]["routes"][0]["body_mb"] = 1
        self.apply_config()
        self.assertEqual(request(self.port, method="POST", body="a" * (1048576 + 1))[0], 413)
        self.config["hosts"][0]["routes"][0]["targets"][0]["address"] = "not-running.invalid"
        result = self.apply_config()
        self.assertFalse(result["pending"])
        self.assertTrue(self.store.status()["healthy"])

    def test_draft_conflicts_history_restore_and_restart(self):
        original = self.store.snapshot()
        self.config["hosts"][0]["routes"][0]["targets"][0]["port"] = self.backends[1].server_port
        saved = self.store.save(self.config, original["revision"])
        self.assertTrue(saved["pending"])
        self.assertEqual(json.loads(request(self.port)[2])["server"], self.backends[0].server_port)
        with self.assertRaises(ConflictError):
            self.store.save(self.config, original["revision"])
        self.store.apply(saved["revision"])
        self.assertEqual(json.loads(request(self.port)[2])["server"], self.backends[1].server_port)
        restored = self.store.restore(original["generation"], saved["revision"])
        self.assertTrue(restored["pending"])
        self.store.apply(restored["revision"])
        self.assertEqual(json.loads(request(self.port)[2])["server"], self.backends[0].server_port)
        # Draft must survive restart without silently becoming active.
        saved = self.store.save(self.config, self.store.snapshot()["revision"])
        self.store.stop()
        self.store.start()
        self.assertTrue(self.store.snapshot()["pending"])
        self.assertEqual(json.loads(request(self.port)[2])["server"], self.backends[0].server_port)
        self.assertTrue(self.store.history()[0]["active"])

    def test_nginx_syntax_failure_preserves_active(self):
        before = self.store.active_id()
        real_prepare = self.store.prepare

        def malformed(config):
            generation = real_prepare(config)
            atomic_write(self.store.revisions / generation / "nginx.conf", "this is not nginx;\n")
            return generation

        with patch.object(self.store, "prepare", side_effect=malformed), self.assertRaises(ApplyError):
            self.store.apply(self.store.snapshot()["revision"])
        self.assertEqual(self.store.active_id(), before)
        self.assertTrue(self.store.status()["healthy"])
        self.assertEqual(request(self.port)[0], 200)

    def test_failed_reload_confirmation_rolls_back(self):
        before = self.store.active_id()
        real_wait = self.store.wait_generation

        def reject_new(generation, timeout=8):
            if generation != before:
                time.sleep(.2)
                return False
            return real_wait(generation, timeout)

        with patch.object(self.store, "wait_generation", side_effect=reject_new), self.assertRaises(ApplyError):
            self.store.apply(self.store.snapshot()["revision"])
        self.assertEqual(self.store.active_id(), before)
        self.assertTrue(self.store.status()["healthy"])

    def test_admin_auth_csrf_export_health_and_password(self):
        port = self.admin.server_port
        self.assertEqual(request(port, "/api/config")[0], 401)
        self.assertEqual(request(port, "/api/dashboard")[0], 401)
        self.assertEqual(request(port, "/api/docker")[0], 401)
        self.assertEqual(request(port, "/api/docker/labels")[0], 401)
        self.assertEqual(request(port, "/healthz")[0], 200)
        self.assertEqual(request(port, "/")[0], 200)
        headers = {"Content-Type": "application/json"}
        body = json.dumps({"password": "test-password-12345"})
        self.assertEqual(request(port, "/api/login", "POST", {**headers,"Origin":"https://evil.test"}, body)[0], 403)
        status, response_headers, data = request(port, "/api/login", "POST", headers, body)
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", response_headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", response_headers["Set-Cookie"])
        cookie = response_headers["Set-Cookie"].split(";")[0]
        headers["Cookie"] = cookie
        status, _, labels = request(port, "/api/docker/labels", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(labels)["mode"], "off")
        label_body = json.dumps({"action": "scan"})
        self.assertEqual(request(port, "/api/docker/labels", "POST", headers, label_body)[0], 403)
        self.assertEqual(request(port, "/api/export", headers=headers)[0], 200)
        status, _, dashboard = request(port, "/api/dashboard", headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(dashboard)["available"])
        status, _, docker = request(port, "/api/docker", headers=headers)
        self.assertEqual(status, 200)
        docker = json.loads(docker)
        docker_settings = {**docker["settings"], "enabled": True,
                           "socket_path": str(self.root / "missing-docker.sock")}
        docker_body = json.dumps({"settings": docker_settings, "revision": docker["revision"]})
        self.assertEqual(request(port, "/api/docker", "POST", headers, docker_body)[0], 403)
        body = json.dumps({"revision": self.store.snapshot()["revision"]})
        self.assertEqual(request(port, "/api/test", "POST", headers, body)[0], 403)
        headers["X-CSRF-Token"] = json.loads(data)["csrf"]
        self.assertEqual(request(port, "/api/docker/labels", "POST", headers, label_body)[0], 200)
        self.assertEqual(request(port, "/api/docker/labels", "POST", headers, '{"action":"apply"}')[0], 400)
        before = self.store.snapshot()
        status, _, docker = request(port, "/api/docker", "POST", headers, docker_body)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(docker)["connected"])
        self.assertEqual(self.store.snapshot(), before)
        self.assertTrue(self.store.status()["healthy"])
        self.assertEqual(request(port, "/api/test", "POST", headers, body)[0], 200)
        body = json.dumps({"current": "test-password-12345", "password": "new-test-password-12345"})
        self.assertEqual(request(port, "/api/password", "POST", headers, body)[0], 200)
        self.assertEqual(request(port, "/api/config", headers=headers)[0], 401)
        # Restore fixture password for repeatability.
        self.auth.set_password("test-password-12345")

    def test_general_address_api_requires_session_csrf_and_http_confirmation(self):
        port = self.admin.server_port
        original = self.store.snapshot()
        settings = self.admin.settings.snapshot()
        cookie, session = self.auth.login("test-password-12345", "general-settings-test")
        headers = {"Cookie": "ambergate_session=" + cookie, "Content-Type": "application/json"}
        body = dict(settings={"public_url": "10.0.0.10"}, revision=settings["revision"])
        self.assertEqual(request(port, "/api/settings")[0], 401)
        self.assertEqual(request(port, "/api/settings", "POST", headers, json.dumps(body))[0], 403)
        headers["X-CSRF-Token"] = session["csrf"]
        try:
            status, _, data = request(port, "/api/settings", "POST", headers, json.dumps(body))
            self.assertEqual(status, 400)
            warning = json.loads(data)
            self.assertEqual(warning["code"], "http_confirmation_required")
            self.assertEqual(warning["public_url"], "http://10.0.0.10:8083")
            self.assertEqual(self.admin.settings.snapshot(), settings)
            status, _, data = request(port, "/api/settings", "POST", headers, json.dumps({**body, "confirm_http": True}))
            self.assertEqual(status, 200)
            saved = json.loads(data)
            self.assertTrue(saved["settings"]["allow_http"])
            self.assertEqual(json.loads(request(port, "/api/dashboard", headers=headers)[2])["general"], saved)
            self.assertEqual(self.store.snapshot(), original)
            self.assertEqual(request(port, "/api/settings", "POST", headers, json.dumps(body))[0], 409)
        finally:
            self.admin.settings.save({"public_url": settings["settings"]["public_url"]}, self.admin.settings.snapshot()["revision"], True)
            self.auth.logout(cookie)

    def test_dashboard_observes_traffic_cache_errors_and_excludes_admin_probes(self):
        self.config["hosts"][0]["domain"] = "telemetry.test"
        self.config["hosts"][0]["routes"][0]["cache"] = True
        self.config["hosts"][0]["routes"].append(route("/broken", free_port(), "broken"))
        self.config["hosts"][0]["routes"].append(route("/limited", self.backends[0].server_port, "limited"))
        self.config["hosts"][0]["routes"][-1].update(rate_rps=1, rate_burst=0)
        self.apply_config()
        headers = {"Host": "telemetry.test"}
        self.assertEqual(request(self.port, "/dashboard-metric?token=secret-query", headers=headers)[0], 200)
        self.assertEqual(request(self.port, "/dashboard-metric?token=secret-query", headers=headers)[0], 200)
        # Signed query bypasses cache; plain URL gives one MISS and one HIT.
        self.assertEqual(request(self.port, "/public-metric", headers=headers)[0], 200)
        self.assertEqual(request(self.port, "/public-metric", headers=headers)[0], 200)
        self.assertEqual(request(self.port, "/broken", headers=headers)[0], 502)
        self.assertEqual(request(self.port, "/limited", headers=headers)[0], 200)
        self.assertEqual(request(self.port, "/limited", headers=headers)[0], 429)
        self.assertEqual(request(self.port, "/unknown?password=secret-query", headers={"Host": "random-attacker.test"})[0], 404)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            data = self.store.dashboard()
            if data["domains"].get("telemetry.test", {}).get("requests") == 7:
                break
            time.sleep(.02)
        stats = data["domains"]["telemetry.test"]
        self.assertTrue(data["available"])
        self.assertEqual(stats["requests"], 7)
        self.assertEqual(stats["statuses"], [0, 5, 0, 1, 1])
        self.assertEqual(stats["limited"], 1)
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["cache_lookups"], 2)
        self.assertIsNotNone(data["connections"])
        self.assertNotIn("secret-query", json.dumps(data))
        self.assertNotIn("random-attacker.test", json.dumps(data))
        self.assertTrue(any(e["route"] == "/broken" and e["status"] == 502 for e in data["events"]))
        count = data["summary"]["requests"]
        for _ in range(3):
            self.store.dashboard()
            request(self.admin.server_port, "/healthz")
        self.assertEqual(self.store.dashboard()["summary"]["requests"], count)

    def test_sse_pushes_real_traffic_and_applied_configuration(self):
        from .test_events import read_event
        token, _ = self.auth.login("test-password-12345", "sse-test")
        client = http.client.HTTPConnection("127.0.0.1", self.admin.server_port, timeout=5)
        response = None
        try:
            client.request("GET", "/api/events", headers={"Cookie": "ambergate_session=" + token})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            _, before, _ = read_event(response)
            self.assertEqual(request(self.port, "/sse-live-traffic")[0], 200)
            self.config["hosts"][0]["domain"] = "sse-updated.test"
            self.apply_config()
            for _ in range(5):
                name, after, _ = read_event(response)
                if (name == "dashboard" and after["summary"]["requests"] > before["summary"]["requests"]
                        and after["configuration"]["generation"] == self.store.active_id()):
                    break
            self.assertGreater(after["summary"]["requests"], before["summary"]["requests"])
            self.assertEqual(after["configuration"]["hosts"][0]["domain"], "sse-updated.test")
            self.assertTrue(after["nginx"]["healthy"])
        finally:
            self.auth.logout(token)
            if response:
                response.close()
            client.close()

    def test_runtime_upgrade_instruments_active_settings_and_preserves_draft(self):
        self.store.stop()
        old_id = self.store.active_id()
        active = self.store.active_config()
        legacy = (self.store.active / "nginx.conf").read_text()
        legacy = "\n".join(line for line in legacy.splitlines()
                           if "access_log \"syslog:" not in line and "location = /status" not in line) + "\n"
        atomic_write(self.store.active / "nginx.conf", legacy)
        draft = copy.deepcopy(active)
        draft["hosts"][0]["domain"] = "unapplied.test"
        self.store.save(draft, self.store.snapshot()["revision"])
        with patch.object(self.store, "check", side_effect=ApplyError("upgrade rejected")):
            with self.assertRaises(ApplyError):
                self.store.start()
        self.assertEqual(self.store.active_id(), old_id)
        self.assertEqual(self.store.draft_config(), draft)
        self.store.start()
        self.assertNotEqual(self.store.active_id(), old_id)
        self.assertEqual(self.store.active_config(), active)
        self.assertEqual(self.store.draft_config(), draft)
        self.assertTrue(self.store.snapshot()["pending"])
        self.assertIsNotNone(self.store.dashboard()["connections"])
        self.assertEqual(request(self.port)[0], 200)
        self.assertEqual(request(self.port, headers={"Host": "unapplied.test"})[0], 404)


if __name__ == "__main__":
    unittest.main()
