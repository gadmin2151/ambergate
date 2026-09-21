import copy
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest

from gateway.docker import Docker, settings
from gateway.nginx import digest
from gateway.storage import ConflictError


def container(name="backend_1", identity="b" * 64, state="running", network="applications", ports=None):
    return dict(Id=identity, Names=["/" + name], Image="example/backend:1", State=state,
                Status="Up 10 minutes (healthy)",
                Labels={"com.docker.compose.project": "example", "com.docker.compose.service": "backend",
                        "secret-label": "never-send-secret"},
                Ports=ports if ports is not None else [{"PrivatePort": 8000, "Type": "tcp"}],
                NetworkSettings={"Networks": {network: {"NetworkID": network + "-id", "IPAddress": "172.30.0.12"}}})


class FakeDockerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.calls.append(("GET", self.path))
        if self.path == "/version":
            payload = {"Version": "28.0.0", "ApiVersion": "1.48"}
        elif self.path == "/v1.48/containers/json?all=1&limit=500":
            payload = self.server.containers
        elif self.path == "/v1.48/containers/gateway-self/json":
            payload = self.server.own
        else:
            self.send_error(404)
            return
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class DockerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gw-docker-")
        self.root = Path(self.temp.name)
        self.path = str(self.root / "docker.sock")
        self.server = socketserver.UnixStreamServer(self.path, FakeDockerHandler)
        self.server.calls = []
        self.server.containers = [container(), container("gateway-self", "a" * 64),
                                  container("stopped", "c" * 64, "exited"),
                                  container("isolated", "d" * 64, network="other")]
        self.server.own = {"Id": "a" * 64, "NetworkSettings": {"Networks": {
            "applications": {"NetworkID": "applications-id", "IPAddress": "172.30.0.10"}}},
            "Config": {"Env": ["SECRET=never-send-secret"]}, "Mounts": [{"Source": "never-send-secret"}]}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.docker = Docker(self.root, in_container=True)
        self.value = dict(enabled=True, socket_path=self.path, gateway_container="gateway-self")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def connect(self):
        return self.docker.save(self.value, digest(self.docker.read()))

    def test_discovery_uses_only_get_and_never_exposes_private_metadata(self):
        data = self.connect()
        self.assertTrue(data["connected"])
        rows = {c["name"]: c for c in data["containers"]}
        self.assertEqual(rows["backend_1"]["endpoints"], [{"address": "backend_1", "kind": "dns"}])
        self.assertTrue(rows["backend_1"]["selectable"])
        self.assertFalse(rows["gateway-self"]["selectable"])
        self.assertFalse(rows["stopped"]["selectable"])
        self.assertFalse(rows["isolated"]["selectable"])
        self.assertNotIn("never-send-secret", json.dumps(data))
        self.assertEqual(self.server.calls, [("GET", "/version"),
            ("GET", "/v1.48/containers/json?all=1&limit=500"),
            ("GET", "/v1.48/containers/gateway-self/json")])

    def test_missing_or_non_socket_is_reported_without_crashing_gateway(self):
        self.value["socket_path"] = str(self.root / "missing.sock")
        self.assertFalse(self.connect()["connected"])
        Path(self.value["socket_path"]).write_text("not a socket")
        self.assertIn("не является Unix socket", self.docker.snapshot(force=True)["message"])
        self.assertEqual(self.server.calls, [])

    def test_settings_persist_conflicts_are_rejected_and_disable_preserves_path(self):
        revision = digest(self.docker.read())
        self.connect()
        self.assertEqual(self.file_mode(), 0o600)
        restored = Docker(self.root, in_container=True)
        self.assertEqual(restored.read(), self.value)
        with self.assertRaises(ConflictError):
            restored.save(self.value, revision)
        self.value["enabled"] = False
        before = len(self.server.calls)
        result = self.connect()
        self.assertFalse(result["connected"])
        self.assertEqual(len(self.server.calls), before)
        self.assertEqual(restored.read()["socket_path"], self.path)

    def file_mode(self):
        return os.stat(self.root / "docker.json").st_mode & 0o777

    def test_native_gateway_selects_published_ports_not_container_dns_or_internal_ports(self):
        self.docker.in_container = False
        self.value["gateway_container"] = ""
        self.server.containers = [container(ports=[
            {"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "0.0.0.0"},
            {"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp", "IP": "::"},
            {"PrivatePort": 53, "PublicPort": 5353, "Type": "udp", "IP": "0.0.0.0"}]),
            container("unpublished", "f" * 64)]
        data = self.connect()
        rows = {c["name"]: c for c in data["containers"]}
        self.assertEqual(rows["backend_1"]["endpoints"][0],
                         dict(address="127.0.0.1", port=8080, container_port=80, kind="published"))
        self.assertEqual(rows["backend_1"]["ports"], [80])
        self.assertFalse(rows["unpublished"]["selectable"])
        self.assertEqual(len(self.server.calls), 2)

    def test_default_bridge_and_ipv6_only_use_explicit_ip_not_unresolvable_dns(self):
        self.server.own["NetworkSettings"]["Networks"] = {"bridge": {"NetworkID": "bridge-id"}}
        self.server.containers = [container(network="bridge")]
        row = self.connect()["containers"][0]
        self.assertEqual(row["endpoints"], [dict(address="172.30.0.12", kind="ip", network="bridge")])
        self.server.own["NetworkSettings"]["Networks"] = {"applications": {"NetworkID": "applications-id"}}
        self.server.containers = [container()]
        self.server.containers[0]["NetworkSettings"]["Networks"]["applications"].update(IPAddress="", GlobalIPv6Address="fd00::2")
        row = self.docker.snapshot(force=True)["containers"][0]
        self.assertEqual(row["endpoints"][0]["address"], "fd00::2")
        self.assertEqual(row["endpoints"][0]["kind"], "ip")

    def test_missing_expose_allows_manual_port_on_a_shared_network(self):
        self.server.containers = [container(ports=[])]
        row = self.connect()["containers"][0]
        self.assertTrue(row["selectable"])
        self.assertEqual(row["ports"], [])

    def test_bad_self_identity_does_not_assume_all_networks_are_reachable(self):
        self.value["gateway_container"] = "custom-hostname"
        data = self.connect()
        self.assertTrue(data["connected"])
        self.assertIn("Укажите", data["message"])
        self.assertFalse(any(c["selectable"] for c in data["containers"]))

    def test_refresh_removes_stopped_targets_and_malformed_response_is_not_connected(self):
        self.connect()
        self.server.containers[0]["State"] = "exited"
        data = self.docker.snapshot(force=True)
        self.assertFalse(any(c["selectable"] for c in data["containers"]))
        self.server.containers = [None]
        data = self.docker.snapshot(force=True)
        self.assertFalse(data["connected"])
        self.assertEqual(data["containers"], [])

    def test_local_socket_configuration_rejects_remote_urls_and_injection(self):
        for key, bad in (("socket_path", "tcp://host:2375"), ("socket_path", "/tmp/a\nsock.sock"),
                         ("socket_path", "/tmp/../secret.sock"), ("gateway_container", "name/json?all=1"),
                         ("enabled", "yes")):
            invalid = copy.deepcopy(self.value)
            invalid[key] = bad
            with self.subTest(key=key, value=bad), self.assertRaises(ValueError):
                settings(invalid)
