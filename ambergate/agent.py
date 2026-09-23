"""Outbound-only Docker discovery agent and streaming reverse tunnel."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

from .agents import TOKEN, MANUAL_TARGET, agent_routes, wire_snapshot
from .docker import Docker, settings
from .storage import write_json
from .tunnel import bridge, close_tcp, connect, server_url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Agent redirects are disabled", headers, fp)


class Agent:
    def __init__(self, origin, token, docker, ca_file=None, allow_http=False, interval=5, health=None):
        server_url(origin, allow_http)
        if not TOKEN.fullmatch(token):
            raise ValueError("Invalid AMBERGATE_AGENT_TOKEN")
        if not 2 <= interval <= 30:
            raise ValueError("Agent interval must be 2–30 seconds")
        self.origin, self.token, self.docker = origin.rstrip("/"), token, docker
        self.ca_file, self.allow_http, self.interval = ca_file, allow_http, interval
        self.health = health
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.control, self.targets, self.streams = None, {}, {}
        self.containers = {}
        self.slots = threading.BoundedSemaphore(32)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))

    def report(self, snapshot):
        payload = json.dumps(snapshot, separators=(",", ":")).encode()
        if len(payload) > 1048576:
            snapshot = dict(version=1, connected=False, truncated=True, engine_version="",
                            message="Agent inventory exceeds 1 MiB", containers=[])
            payload = json.dumps(snapshot).encode()
        request = urllib.request.Request(self.origin + "/api/agents/report", data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token,
                     "X-AmberGate-Manual-Routing": "1"})
        with self.opener.open(request, timeout=10) as response:
            value = response.read(65537)
            if len(value) > 65536:
                raise ValueError("Agent report response is too large")
            return json.loads(value)

    def collect(self):
        try:
            snapshot = wire_snapshot(self.docker.snapshot(force=True))
        except (OSError, ValueError, KeyError, TypeError):
            snapshot = dict(version=1, connected=False, truncated=False, engine_version="",
                            message="Invalid Docker inventory", containers=[])
        routes, _ = agent_routes(snapshot)
        with self.lock:
            self.targets = {r["target_id"]: (r["address"], r["spec"]["port"]) for r in routes}
            self.containers = {c["id"]: c["endpoints"][0]["address"] for c in snapshot["containers"]
                               if c["state"] == "running" and not c["is_gateway"] and c["endpoints"] and c["shared_networks"]}
            if not snapshot["connected"] or snapshot["truncated"]:
                self.containers = {}
        return snapshot

    def destination(self, target):
        """The central server may choose a port, never an arbitrary address."""
        match = MANUAL_TARGET.fullmatch(target)
        if match:
            address, port = self.containers.get(match[1]), int(match[2])
            return (address, port) if address and port <= 65535 else None
        return self.targets.get(target)

    def open_stream(self, identity, target, control):
        connection, ws = None, None
        try:
            with self.lock:
                destination = self.destination(target)
            if not destination or control is not self.control or self.stopping.is_set():
                raise ValueError("Agent target is unavailable")
            connection = socket.create_connection(destination, timeout=5)
            ws = connect(self.origin, "/api/agents/tunnel/" + identity, self.token, self.ca_file, self.allow_http)
            with self.lock:
                if control is not self.control or control.closed.is_set() or self.stopping.is_set():
                    raise OSError("Agent disconnected")
                self.streams[identity] = (ws, connection)
            bridge(ws, connection)
        except (OSError, ValueError, EOFError):
            try:
                control.send_json({"type": "failed", "id": identity})
            except OSError:
                pass
        finally:
            if ws:
                ws.close()
            if connection:
                close_tcp(connection)
            with self.lock:
                self.streams.pop(identity, None)
            self.slots.release()

    def receive(self, control):
        try:
            while not self.stopping.is_set():
                opcode, payload = control.receive()
                value = json.loads(payload) if opcode == 1 else None
                if (not isinstance(value, dict) or set(value) != {"type", "id", "target"} or value["type"] != "open"
                        or not isinstance(value["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["id"])
                        or not isinstance(value["target"], str)
                        or not (re.fullmatch(r"[a-f0-9]{32}", value["target"]) or MANUAL_TARGET.fullmatch(value["target"]))):
                    raise ValueError("Invalid tunnel control message")
                if self.slots.acquire(blocking=False):
                    threading.Thread(target=self.open_stream, args=(value["id"], value["target"], control),
                                     name="agent-stream", daemon=True).start()
                else:
                    control.send_json({"type": "failed", "id": value["id"]})
        except (OSError, ValueError, EOFError):
            pass
        finally:
            control.close()

    def disconnect(self):
        with self.lock:
            control, streams = self.control, list(self.streams.values())
            self.control = None
        if control:
            control.close()
        for ws, connection in streams:
            ws.close()
            close_tcp(connection)

    def stop(self):
        self.stopping.set()
        self.disconnect()

    def run(self):
        delay = 1
        last_status = None
        while not self.stopping.is_set():
            try:
                control = connect(self.origin, "/api/agents/connect", self.token, self.ca_file, self.allow_http)
                with self.lock:
                    self.control = control
                thread = threading.Thread(target=self.receive, args=(control,), name="agent-control", daemon=True)
                thread.start()
                while not self.stopping.is_set() and not control.closed.is_set():
                    snapshot = self.collect()
                    response = self.report(snapshot)
                    if self.health:
                        write_json(self.health, {"sent_at": time.time(), "healthy": response.get("ok", False)})
                    status = (response.get("ok"), len(snapshot["containers"]))
                    if status != last_status:
                        print(f"AmberGate Agent: tunnel connected, containers={status[1]}, inventory accepted={status[0]}", flush=True)
                        last_status = status
                    delay = 1
                    if self.stopping.wait(self.interval):
                        break
            except (OSError, ValueError, EOFError, urllib.error.URLError) as exc:
                # Never print headers, request bodies, URL credentials or tokens.
                print(f"AmberGate Agent: connection unavailable ({type(exc).__name__}); retrying in {delay}s", flush=True)
            finally:
                self.disconnect()
            if self.stopping.wait(delay):
                break
            delay = min(30, delay * 2)


def main():
    parser = argparse.ArgumentParser(description="AmberGate outbound Docker agent")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    health = Path(os.environ.get("AMBERGATE_AGENT_HEALTH_FILE", "/tmp/ambergate-agent-health.json"))
    if args.healthcheck:
        try:
            value = json.loads(health.read_text())
            return 0 if value["healthy"] and 0 <= time.time() - value["sent_at"] < 90 else 1
        except (OSError, ValueError, KeyError):
            return 1
    token_file = os.environ.get("AMBERGATE_AGENT_TOKEN_FILE")
    token = Path(token_file).read_text().strip() if token_file else os.environ.get("AMBERGATE_AGENT_TOKEN", "")
    docker = Docker(Path("/tmp/ambergate-agent"), in_container=True)
    docker.defaults = settings(dict(enabled=True, socket_path=os.environ.get("AMBERGATE_DOCKER_SOCKET", "/var/run/docker.sock"),
        gateway_container=os.environ.get("AMBERGATE_DOCKER_CONTAINER", ""), host_address=""))
    agent = Agent(os.environ.get("AMBERGATE_SERVER_URL", ""), token, docker,
        ca_file=os.environ.get("AMBERGATE_AGENT_CA_FILE") or None,
        allow_http=os.environ.get("AMBERGATE_AGENT_ALLOW_HTTP", "false").lower() == "true",
        interval=int(os.environ.get("AMBERGATE_AGENT_INTERVAL", "5")), health=health)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: agent.stop())
    agent.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"AmberGate Agent startup failed: {type(exc).__name__}: {exc}", flush=True)
        raise SystemExit(1)
