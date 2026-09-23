"""Authenticated reverse-tunnel transport. One WebSocket per upstream TCP stream.

No HTTP requests or bodies are buffered: Nginx owns HTTP semantics and limits.
The private protocol uses unfragmented binary messages, bounded to 64 KiB.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 65536


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


class WebSocket:
    def __init__(self, connection, reader=None, client=False):
        self.socket = connection
        self.reader = reader or connection.makefile("rb")
        self.client = client
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.socket.settimeout(60)

    def exact(self, size):
        data = self.reader.read(size)
        if len(data) != size:
            raise EOFError("Tunnel disconnected")
        return data

    def send(self, payload, opcode=2):
        if len(payload) > MAX_FRAME or self.closed.is_set():
            raise OSError("Tunnel frame unavailable")
        mask = secrets.token_bytes(4) if self.client else b""
        size = len(payload)
        head = bytes([0x80 | opcode, (0x80 if mask else 0) | (size if size < 126 else 126 if size < 65536 else 127)])
        if size >= 65536:
            head += struct.pack("!Q", size)
        elif size >= 126:
            head += struct.pack("!H", size)
        if mask:
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        with self.lock:
            self.socket.sendall(head + mask + payload)

    def send_json(self, value):
        self.send(json.dumps(value, separators=(",", ":")).encode(), 1)

    def receive(self):
        while not self.closed.is_set():
            first, second = self.exact(2)
            opcode, masked = first & 15, bool(second & 128)
            if first & 0x70 or not first & 0x80 or masked == self.client or opcode not in (1, 2, 8, 9, 10):
                raise ValueError("Invalid tunnel frame")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            if size > MAX_FRAME or opcode >= 8 and size > 125:
                raise ValueError("Tunnel frame too large")
            mask = self.exact(4) if masked else b""
            payload = self.exact(size)
            if mask:
                payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
            if opcode == 8:
                raise EOFError("Tunnel closed")
            if opcode == 9:
                self.send(payload, 10)
            elif opcode != 10:
                return opcode, payload
        raise EOFError("Tunnel closed")

    def heartbeat(self):
        def ping():
            while not self.closed.wait(20):
                try:
                    self.send(b"ag", 9)
                except OSError:
                    self.close()
        threading.Thread(target=ping, name="tunnel-heartbeat", daemon=True).start()

    def close(self):
        if not self.closed.is_set():
            self.closed.set()
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
            self.reader.close()


def upgrade(handler):
    headers = handler.headers
    key = headers.get("Sec-WebSocket-Key", "")
    try:
        valid_key = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        valid_key = False
    if (not valid_key or headers.get("Sec-WebSocket-Version") != "13"
            or headers.get("Upgrade", "").lower() != "websocket"
            or "upgrade" not in headers.get("Connection", "").lower().replace(" ", "").split(",")
            or headers.get("Origin")):
        raise ValueError("Expected an agent WebSocket upgrade")
    handler.protocol_version = "HTTP/1.1"
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True
    ws = WebSocket(handler.connection, handler.rfile)
    ws.heartbeat()
    return ws


def server_url(value, allow_http=False):
    url = urlsplit(value)
    if (url.scheme not in (("https", "http") if allow_http else ("https",)) or not url.hostname
            or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment
            or any(ord(c) < 33 for c in value)):
        raise ValueError("AMBERGATE_SERVER_URL must be an HTTPS origin without a path or credentials")
    _ = url.port  # Reject malformed/out-of-range ports before sending credentials.
    return url


def connect(origin, path, token, ca_file=None, allow_http=False):
    url = server_url(origin, allow_http)
    raw = socket.create_connection((url.hostname, url.port or (443 if url.scheme == "https" else 80)), timeout=10)
    connection = raw
    reader = None
    try:
        if url.scheme == "https":
            connection = ssl.create_default_context(cafile=ca_file).wrap_socket(raw, server_hostname=url.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {path} HTTP/1.1\r\nHost: {url.netloc}\r\nAuthorization: Bearer {token}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n\r\n")
        connection.sendall(request.encode())
        reader = connection.makefile("rb")
        status = reader.readline(4096).decode("ascii", "replace").split()
        if len(status) < 2 or status[1] != "101":
            raise ConnectionError("Tunnel handshake rejected: HTTP " + (status[1] if len(status) > 1 else "invalid"))
        headers, total = {}, 0
        while True:
            line = reader.readline(4096)
            total += len(line)
            if total > 16384 or not line:
                raise ConnectionError("Invalid tunnel handshake headers")
            if line == b"\r\n":
                break
            name, sep, value = line.decode("ascii", "replace").partition(":")
            if not sep or name.lower() in headers:
                raise ConnectionError("Invalid tunnel handshake")
            headers[name.lower()] = value.strip()
        if (headers.get("sec-websocket-accept") != accept_key(key)
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in headers.get("connection", "").lower()):
            raise ConnectionError("Invalid tunnel handshake")
        ws = WebSocket(connection, reader, client=True)
        ws.heartbeat()
        return ws
    except BaseException:
        if reader:
            reader.close()
        connection.close()
        raw.close()
        raise


def close_tcp(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def bridge(ws, connection):
    """Bounded socket backpressure in both directions, including WebSocket apps."""
    connection.settimeout(None)

    def upload():
        try:
            while not ws.closed.is_set():
                data = connection.recv(32768)
                if not data:
                    break
                ws.send(data)
        except (OSError, EOFError):
            pass
        finally:
            ws.close()
            close_tcp(connection)

    thread = threading.Thread(target=upload, name="tunnel-tcp", daemon=True)
    thread.start()
    try:
        while not ws.closed.is_set():
            opcode, data = ws.receive()
            if opcode != 2:
                raise ValueError("Expected binary tunnel data")
            connection.sendall(data)
    except (OSError, EOFError, ValueError):
        pass
    finally:
        ws.close()
        close_tcp(connection)
        thread.join(timeout=2)
