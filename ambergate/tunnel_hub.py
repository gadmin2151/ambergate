"""Loopback-only Nginx upstreams backed by agent-initiated WebSockets."""
import json
from contextlib import contextmanager
import secrets
import selectors
import socket
import threading
import time

from .storage import write_json
from .environment import setting
from .tunnel import bridge, close_tcp


class TunnelHub:
    capacity = 1024
    agent_capacity = 128

    def __init__(self, data, config_paths=None):
        self.agent_capacity = int(setting("AGENT_TARGET_LIMIT", str(self.agent_capacity)))
        if not 1 <= self.agent_capacity <= 512:
            raise ValueError("AGENT_TARGET_LIMIT must be between 1 and 512")
        self.file = data / "agent-ports.json"
        self.lock = threading.RLock()
        self.selector = selectors.DefaultSelector()
        self.listeners, self.controls, self.destinations, self.pending = {}, {}, {}, {}
        self.expires = {}
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(128)
        self.thread = None
        self.ports = json.loads(self.file.read_text()) if self.file.exists() else {}
        try:
            if not isinstance(self.ports, dict) or len(self.ports) > 1024:
                raise ValueError("Invalid persisted agent port mapping")
            for key, port in self.ports.items():
                if not isinstance(key, str) or type(port) is not int or not 1024 <= port <= 65535:
                    raise ValueError("Invalid persisted agent port mapping")
            if config_paths is not None:
                self.prune_unreferenced(data, config_paths)
            for key, port in self.ports.items():
                self.listen(key, port)
        except BaseException:
            self.stop()
            raise

    def prune_unreferenced(self, data, paths):
        """Startup migration: preserve draft, every retained revision and manual selections."""
        referenced = set()
        def visit(value):
            if isinstance(value, dict):
                if value.get("address") == "127.0.0.1" and type(value.get("port")) is int:
                    referenced.add(value["port"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        for path in paths:
            visit(json.loads(path.read_text()))
        manual_file = data / "agent-manual.json"
        manual = json.loads(manual_file.read_text()) if manual_file.exists() else []
        selected = {self.key(t["id"], t["container"], t["port"]) for t in manual}
        kept = {key: port for key, port in self.ports.items() if port in referenced or key in selected}
        if kept != self.ports:
            write_json(self.file, kept)
            self.ports = kept

    def listen(self, key, port):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(32)
            listener.setblocking(False)
            self.selector.register(listener, selectors.EVENT_READ, key)
            self.listeners[key] = listener
            return listener.getsockname()[1]
        except BaseException:
            listener.close()
            raise

    @staticmethod
    def key(agent_id, name, port):
        return json.dumps([agent_id, name, port], separators=(",", ":"))

    def lookup(self, agent_id, name, port):
        with self.lock:
            key = self.key(agent_id, name, port)
            return key, self.ports.get(key)

    @contextmanager
    def reserve(self, targets, retain=None):
        """Preflight a complete authorized batch; failed operations leave no new ports."""
        keys = list(dict.fromkeys(self.key(*target) for target in targets))
        added = {}
        with self.lock:
            new = self.check_capacity(keys)
            try:
                for key in new:
                    added[key] = self.listen(key, 0)
                if added:
                    write_json(self.file, {**self.ports, **added})
                self.ports.update(added)
            except BaseException:
                for key in added:
                    listener = self.listeners.pop(key)
                    self.selector.unregister(listener)
                    listener.close()
                raise
        try:
            yield {key: self.ports[key] for key in keys}
        except BaseException:
            with self.lock:
                # Callers serialize reservations through the agent registry lock.
                try:
                    keep = retain() if retain is not None else set()
                except Exception:
                    # Uncertain commit state: retain mappings rather than break routing.
                    keep = set(added.values())
                dropped = {k for k, port in added.items() if port not in keep}
                remaining = {k: v for k, v in self.ports.items() if k not in dropped}
                if dropped:
                    write_json(self.file, remaining)
                self.ports = remaining
                for key in dropped:
                    listener = self.listeners.pop(key)
                    self.selector.unregister(listener)
                    listener.close()
                    for targets in self.destinations.values():
                        targets.pop(key, None)
            raise

    def check_capacity(self, keys):
        with self.lock:
            new = set(keys) - self.ports.keys()
            if new and len(self.ports) + len(new) > self.capacity:
                raise ValueError(f"Agent tunnel target limit reached ({self.capacity})")
            for identity in {json.loads(key)[0] for key in new}:
                count = sum(json.loads(key)[0] == identity for key in list(self.ports) + list(new))
                if count > self.agent_capacity:
                    raise ValueError(f"Agent tunnel target quota reached ({self.agent_capacity} per agent)")
            return sorted(new)

    def endpoint(self, agent_id, name, port):
        with self.reserve([(agent_id, name, port)]) as ports:
            key = self.key(agent_id, name, port)
            return key, ports[key]

    def targets(self, agent_id, destinations, ttl=None):
        with self.lock:
            self.destinations[agent_id] = dict(destinations)
            self.expires[agent_id] = time.monotonic() + ttl if ttl is not None else float('inf')

    def connected(self, agent_id):
        with self.lock:
            return agent_id in self.controls and not self.controls[agent_id].closed.is_set()

    def disconnect(self, agent_id, current=None):
        with self.lock:
            ws = self.controls.get(agent_id)
            if current is not None and ws is not current:
                return
            self.controls.pop(agent_id, None)
            streams = [p for p in self.pending.values() if p["agent"] == agent_id]
        if ws:
            ws.close()
        for pending in streams:
            self.finish(pending)

    def attach(self, agent_id, ws):
        self.disconnect(agent_id)
        with self.lock:
            if self.stopping.is_set():
                ws.close()
                return
            self.controls[agent_id] = ws

    def control(self, agent_id, ws):
        try:
            while not ws.closed.is_set():
                opcode, payload = ws.receive()
                value = json.loads(payload) if opcode == 1 else None
                if not isinstance(value, dict) or value.get("type") != "failed" or not isinstance(value.get("id"), str):
                    raise ValueError("Invalid agent control message")
                with self.lock:
                    pending = self.pending.get(value["id"])
                    if pending and pending["agent"] == agent_id:
                        self.finish(pending)
        except (OSError, ValueError, EOFError):
            pass
        finally:
            self.disconnect(agent_id, ws)

    def claim(self, agent_id, identity):
        with self.lock:
            pending = self.pending.get(identity)
            if (not pending or pending["agent"] != agent_id or pending["claimed"]
                    or pending["done"].is_set()):
                raise ValueError("Unknown or expired tunnel request")
            pending["claimed"] = True
            return pending

    def data(self, pending, ws):
        with self.lock:
            if pending["done"].is_set():
                ws.close()
                return
            pending["ws"] = ws
            pending["ready"].set()
        try:
            bridge(ws, pending["tcp"])
        finally:
            self.finish(pending)

    @staticmethod
    def unavailable(connection):
        try:
            connection.settimeout(1)
            connection.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass

    def finish(self, pending):
        if not pending["done"].is_set():
            pending["done"].set()
            if pending.get("ws"):
                pending["ws"].close()
            else:
                self.unavailable(pending["tcp"])
            close_tcp(pending["tcp"])
            pending["ready"].set()

    def forward(self, connection, key):
        identity = secrets.token_hex(16)
        pending = None
        try:
            with self.lock:
                agent_id = json.loads(key)[0]
                control = self.controls.get(agent_id)
                target = self.destinations.get(agent_id, {}).get(key)
                count = sum(p["agent"] == agent_id for p in self.pending.values())
                if not control or control.closed.is_set() or not target or count >= 32 or time.monotonic() >= self.expires.get(agent_id, 0):
                    self.unavailable(connection)
                    return
                pending = dict(agent=agent_id, tcp=connection, claimed=False, ws=None,
                               ready=threading.Event(), done=threading.Event())
                self.pending[identity] = pending
            control.send_json(dict(type="open", id=identity, target=target))
            if pending["ready"].wait(10):
                pending["done"].wait()
        except (OSError, ValueError):
            pass
        finally:
            if pending:
                self.finish(pending)
            close_tcp(connection)
            with self.lock:
                self.pending.pop(identity, None)
            self.slots.release()

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, name="agent-listeners", daemon=True)
            self.thread.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                for key, _ in self.selector.select(.5):
                    try:
                        connection, _ = key.fileobj.accept()
                        connection.setblocking(True)
                    except OSError:
                        continue
                    if self.slots.acquire(blocking=False):
                        threading.Thread(target=self.forward, args=(connection, key.data),
                                         name="agent-upstream", daemon=True).start()
                    else:
                        self.unavailable(connection)
                        close_tcp(connection)
            except (OSError, ValueError):
                if not self.stopping.is_set():
                    raise

    def stop(self):
        self.stopping.set()
        with self.lock:
            agents = list(self.controls)
        for agent in agents:
            self.disconnect(agent)
        if self.thread:
            self.thread.join(timeout=2)
        for listener in self.listeners.values():
            listener.close()
        self.selector.close()
