"""Loopback-only Nginx upstreams backed by agent-initiated WebSockets."""
import json
import secrets
import selectors
import socket
import threading

from .storage import write_json
from .tunnel import bridge, close_tcp


class TunnelHub:
    def __init__(self, data):
        self.file = data / "agent-ports.json"
        self.lock = threading.RLock()
        self.selector = selectors.DefaultSelector()
        self.listeners, self.controls, self.destinations, self.pending = {}, {}, {}, {}
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
                self.listen(key, port)
        except BaseException:
            self.stop()
            raise

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

    def endpoint(self, agent_id, name, port):
        # Stable across recreation of a Compose replica with the same name.
        key = json.dumps([agent_id, name, port], separators=(",", ":"))
        with self.lock:
            if key not in self.ports:
                if len(self.ports) >= 1024:
                    raise ValueError("Agent tunnel target limit reached (1024)")
                assigned = self.listen(key, 0)
                try:
                    write_json(self.file, {**self.ports, key: assigned})
                except BaseException:
                    listener = self.listeners.pop(key)
                    self.selector.unregister(listener)
                    listener.close()
                    raise
                self.ports[key] = assigned
            return key, self.ports[key]

    def targets(self, agent_id, destinations):
        with self.lock:
            self.destinations[agent_id] = dict(destinations)

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
                if not control or control.closed.is_set() or not target or count >= 32:
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
