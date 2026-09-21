"""End-to-end discovery and balancing with the real CI Docker daemon.

Run after `docker build -t gateway:ci .`. Creates only disposable containers and
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

IMAGE = os.environ.get("GATEWAY_TEST_IMAGE", "gateway:ci")
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


def backend(name, address, label, network=NETWORK):
    identity = docker("run", "-d", "--name", name, "--network", network,
                      "--ip", address, "--expose", "8000", "--entrypoint", "python3",
                      IMAGE, "-u", "-c", BACKEND, label)
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
        names = [PREFIX + "_backend_1", PREFIX + "_backend_2"]
        first = backend(names[0], str(subnet.network_address + 10), "backend-a")
        backend(names[1], str(subnet.network_address + 11), "backend-b")
        gateway = docker("run", "-d", "--name", PREFIX + "-gateway", "--network", NETWORK,
                         "-p", "127.0.0.1::8083", "-p", "127.0.0.1::80",
                         "-e", "GATEWAY_ADMIN_PASSWORD=docker-discovery-test-password",
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

        def traffic():
            req = urllib.request.Request(f"http://127.0.0.1:{public}/", headers={"Host": "docker.test"})
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

        disconnected = request("/api/docker", {"revision": discovered["revision"],
            "settings": {**discovered["settings"], "enabled": False}})
        assert not disconnected["connected"]
        assert traffic() in {"backend-a-recreated", "backend-b"}
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
