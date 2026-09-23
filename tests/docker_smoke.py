"""End-to-end discovery and balancing with the real CI Docker daemon.

Run after `docker build -t ambergate:ci .`. Creates only disposable containers and
one network, removes all of them on success or failure. No Docker SDK required.
"""
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.helpers import config, route

IMAGE = os.environ.get("AMBERGATE_TEST_IMAGE", "ambergate:ci")
PREFIX = "gw-discovery-" + uuid.uuid4().hex[:8]
NETWORK = PREFIX + "-net"
OWNED = []
BACKEND = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data=sys.argv[1].encode()
        self.send_response(200); self.send_header('Content-Length',str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def log_message(self,*args): pass
HTTPServer(('0.0.0.0',8000), Handler).serve_forever()
"""


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def backend(name, address, label, network=NETWORK, route_label=None):
    extra = ["--label", "ambergate.route=" + route_label] if route_label else []
    identity = docker("run", "-d", "--name", name, "--network", network,
                      "--ip", address, "--expose", "8000", "--entrypoint", "python3",
                      *extra, IMAGE, "-u", "-c", BACKEND, label)
    OWNED.append(identity)
    return identity


def port(identity, private):
    info = json.loads(docker("inspect", identity))[0]
    return int(info["NetworkSettings"]["Ports"][f"{private}/tcp"][0]["HostPort"])


def eventually(predicate, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(.4)
    raise AssertionError("Timed out waiting for Docker gateway")


def main():
    network_created = False
    gateway = None
    try:
        docker("network", "create", NETWORK)
        network_created = True
        subnet = ipaddress.ip_network(json.loads(docker("network", "inspect", NETWORK))[0]["IPAM"]["Config"][0]["Subnet"])
        # Docker allows explicit endpoint IPs only on explicitly configured
        # subnets. Reuse the free subnet just allocated by the daemon.
        docker("network", "rm", NETWORK)
        docker("network", "create", "--subnet", str(subnet), NETWORK)
        names = [PREFIX + "_backend_1", PREFIX + "_backend_2"]
        first = backend(names[0], str(subnet.network_address + 10), "backend-a")
        backend(names[1], str(subnet.network_address + 11), "backend-b")
        gateway = docker("run", "-d", "--name", PREFIX + "-gateway", "--network", NETWORK,
                         "-p", "127.0.0.1::8083", "-p", "127.0.0.1::80",
                         "-e", "AMBERGATE_ADMIN_PASSWORD=docker-discovery-test-password",
                         "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock,readonly",
                         IMAGE)
        OWNED.append(gateway)
        admin, public = port(gateway, 8083), port(gateway, 80)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        csrf = ""

        def request(path, value=None):
            data = json.dumps(value).encode() if value is not None else None
            req = urllib.request.Request(f"http://127.0.0.1:{admin}" + path, data=data,
                                         headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
            with opener.open(req, timeout=12) as response:
                return json.load(response)

        def traffic(host="docker.test", path="/"):
            req = urllib.request.Request(f"http://127.0.0.1:{public}{path}", headers={"Host": host})
            with opener.open(req, timeout=6) as response:
                return response.read().decode()

        eventually(lambda: request("/healthz")["healthy"])
        csrf = request("/api/login", {"password": "docker-discovery-test-password"})["csrf"]
        initial = request("/api/config")
        current = request("/api/docker")
        discovered = request("/api/docker", {"revision": current["revision"], "settings": {
            "enabled": True, "socket_path": "/var/run/docker.sock", "gateway_container": ""}})
        assert discovered["connected"], discovered["message"]
        assert NETWORK in discovered["gateway_networks"]
        rows = {c["name"]: c for c in discovered["containers"]}
        assert not rows[PREFIX + "-gateway"]["selectable"]
        targets = []
        for name in names:
            row = rows[name]
            assert row["selectable"] and 8000 in row["ports"], row
            assert row["endpoints"][0]["kind"] == "dns", row
            targets.append({"address": row["endpoints"][0]["address"], "port": 8000, "weight": 1, "backup": False})
        cfg = config(8000)
        cfg["hosts"][0]["domain"] = "docker.test"
        cfg["hosts"][0]["routes"][0]["targets"] = targets
        saved = request("/api/config", {"config": cfg, "revision": initial["revision"]})
        request("/api/apply", {"revision": saved["revision"]})
        eventually(lambda: traffic() in {"backend-a", "backend-b"})
        assert {traffic() for _ in range(20)} == {"backend-a", "backend-b"}
        print("PASS: real socket discovery, shared network, Docker names with underscores, two-backend balancing")

        # Replace a selected container with the same name but a different IP.
        # The route config is untouched; Nginx must follow Docker DNS updates.
        docker("rm", "-f", first)
        OWNED.remove(first)
        backend(names[0], str(subnet.network_address + 12), "backend-a-recreated")
        eventually(lambda: traffic() == "backend-a-recreated", seconds=45)
        assert request("/api/config")["revision"] == saved["revision"]
        print("PASS: container recreation at another IP without changing or applying a route")

        # A container in the default bridge is reached through its published
        # host port, without adding either container to another network.
        isolated = docker("run", "-d", "--name", PREFIX + "-isolated", "--network", "bridge",
                          "-p", "0.0.0.0::8000", "--entrypoint", "python3",
                          IMAGE, "-u", "-c", BACKEND, "backend-host-port")
        OWNED.append(isolated)
        host_address = json.loads(docker("inspect", gateway))[0]["NetworkSettings"]["Networks"][NETWORK]["Gateway"]
        discovered = request("/api/docker", {"revision": discovered["revision"],
            "settings": {**discovered["settings"], "host_address": host_address}})
        row = next(c for c in discovered["containers"] if c["id"] == isolated)
        assert row["selectable"] and not row["shared_networks"], row
        target = row["endpoints"][0]
        assert target["address"] == host_address and target["port"] == port(isolated, 8000), target
        cfg["hosts"][0]["routes"][0]["targets"] = [{"address": target["address"],
            "port": target["port"], "weight": 1, "backup": False}]
        saved = request("/api/config", {"config": cfg, "revision": saved["revision"]})
        request("/api/apply", {"revision": saved["revision"]})
        eventually(lambda: traffic() == "backend-host-port")
        print("PASS: container in a different Docker network via host IP and published port")

        # Labels are opt-in; preview changes first, then enable the background
        # reconciler. These are additional disposable containers, not user apps.
        label = "host=labels.test;path=/api;port=8000;strip=true;balance=least_conn"
        replicas = [backend(PREFIX + "-label-" + str(i), str(subnet.network_address + 20 + i),
                            "label-" + str(i), route_label=label) for i in range(2)]
        policy = request("/api/docker/labels")
        policy = request("/api/docker/labels", {"action": "settings", "mode": "preview", "revision": policy["revision"]})
        preview = request("/api/docker/labels", {"action": "scan"})
        assert not preview["errors"] and len(preview["changes"]) == 1, preview
        assert request("/api/config")["revision"] == saved["revision"]
        applied = request("/api/docker/labels", {"action": "apply", "token": preview["token"]})
        assert not applied["errors"], applied
        eventually(lambda: traffic("labels.test", "/api/") in {"label-0", "label-1"})
        assert {traffic("labels.test", "/api/") for _ in range(20)} == {"label-0", "label-1"}
        policy = request("/api/docker/labels", {"action": "settings", "mode": "auto", "revision": policy["revision"]})

        def label_targets():
            active = request("/api/dashboard")["configuration"]["hosts"]
            return next(h for h in active if h["domain"] == "labels.test")["routes"][0]["docker"]["targets"]

        # Save a draft and scale down: only label changes may reach live Nginx.
        current = request("/api/config")
        draft = current["config"]
        draft["settings"]["body_mb"] = 321
        request("/api/config", {"config": draft, "revision": current["revision"]})
        docker("stop", "--time", "1", replicas[0])
        eventually(lambda: len(label_targets()) == 1)
        assert traffic("labels.test", "/api/") == "label-1"
        assert request("/api/dashboard")["configuration"]["settings"]["body_mb"] != 321
        assert request("/api/config")["config"]["settings"]["body_mb"] == 321
        docker("stop", "--time", "1", replicas[1])
        eventually(lambda: not label_targets())
        try:
            traffic("labels.test", "/api/")
            raise AssertionError("Empty label route must return 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
        docker("start", replicas[0])
        eventually(lambda: len(label_targets()) == 1)
        eventually(lambda: traffic("labels.test", "/api/") == "label-0")
        assert traffic() == "backend-host-port"
        print("PASS: compact labels, preview, live balancing, automatic stop/start, zero replicas and draft isolation")

        # A label uses the container port, even when Docker publishes a random
        # host port. Multiple named labels on one container are also supported.
        host_labeled = docker("run", "-d", "--name", PREFIX + "-host-label", "--network", "bridge",
            "-p", "0.0.0.0::8000", "--entrypoint", "python3",
            "--label", "ambergate.route.host=host=host-label.test;port=8000;via=host",
            "--label", "ambergate.route.group=group=app-api;port=8000;via=host;weight=2",
            IMAGE, "-u", "-c", BACKEND, "host-label")
        OWNED.append(host_labeled)
        eventually(lambda: traffic("host-label.test") == "host-label")
        current = request("/api/config")
        group_cfg = current["config"]
        manual = group_cfg["hosts"][0]["routes"][0]
        manual["docker"] = dict(managed=False, group="app-api", targets=[])
        saved_group = request("/api/config", {"config": group_cfg, "revision": current["revision"]})
        request("/api/apply", {"revision": saved_group["revision"]})
        eventually(lambda: len(request("/api/dashboard")["configuration"]["hosts"][0]["routes"][0]["docker"]["targets"]) == 1)
        assert {traffic() for _ in range(20)} == {"backend-host-port", "host-label"}
        print("PASS: named labels, via=host published port mapping and group membership alongside manual targets")

        request("/api/docker/labels", {"action": "settings", "mode": "off", "revision": policy["revision"]})

        disconnected = request("/api/docker", {"revision": discovered["revision"],
            "settings": {**discovered["settings"], "enabled": False}})
        assert not disconnected["connected"]
        assert traffic() in {"backend-host-port", "host-label"}
        print("PASS: disabling discovery does not stop configured traffic")
    except Exception:
        if gateway:
            print(docker("logs", "--tail", "60", gateway, check=False), file=sys.stderr)
        raise
    finally:
        for identity in reversed(OWNED):
            docker("rm", "-f", identity, check=False)
        if network_created:
            docker("network", "rm", NETWORK, check=False)


if __name__ == "__main__":
    main()
