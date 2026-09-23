"""Bounded admission and absolute read deadlines for the private HTTP listener."""
import io
import ipaddress
import threading
import time


class TrustedProxies:
    @staticmethod
    def canonical(value):
        address = ipaddress.ip_address(value)
        return str(address.ipv4_mapped or address) if address.version == 6 else str(address)

    def __init__(self, value=""):
        self.networks = [ipaddress.ip_network(item.strip(), strict=False)
                         for item in value.split(",") if item.strip()]
        if len(self.networks) > 32 or any(net.prefixlen == 0 for net in self.networks):
            raise ValueError("ADMIN_TRUSTED_PROXIES requires at most 32 explicit proxy networks")

    def trusted(self, address):
        ip = ipaddress.ip_address(address)
        return any(ip in net for net in self.networks)

    def client(self, peer, headers):
        # Ignore client-supplied forwarding headers unless the socket peer is trusted.
        if not self.trusted(peer):
            return self.canonical(peer)
        values = headers.get_all("X-Forwarded-For", [])
        if len(values) != 1 or len(values[0]) > 2048:
            return peer
        try:
            chain = values[0].split(",")
            if len(chain) > 32 or any("%" in item for item in chain):
                return peer
            chain = [self.canonical(item.strip()) for item in chain]
        except ValueError:
            return peer
        current = peer
        for address in reversed(chain):
            if not self.trusted(current):
                break
            current = address
        return current


class DeadlineReader(io.RawIOBase):
    """The deadline survives every recv, including reads inside BufferedReader."""
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP request read deadline exceeded")
            self.connection.settimeout(min(15, remaining))
        return self.connection.recv_into(buffer)


class Admission:
    limits = {"pending": 64, "admin": 32, "agent": 160, "report": 16}

    def __init__(self, proxies):
        self.proxies = proxies
        self.lock = threading.Lock()
        self.requests, self.sources = {}, {}
        self.counts = dict.fromkeys(self.limits, 0)

    def enter(self, connection, source):
        with self.lock:
            limit = 64 if self.proxies.trusted(source) else 8
            source = ("peer", source)
            if self.counts["pending"] >= self.limits["pending"] or self.sources.get(source, 0) >= limit:
                return False
            self.requests[connection] = (source, "pending", time.monotonic())
            self.sources[source] = self.sources.get(source, 0) + 1
            self.counts["pending"] += 1
            return True

    def identify(self, connection, client):
        """Charge a verified client identity before any request-body read."""
        with self.lock:
            source, kind, started = self.requests[connection]
            target = ("client", client)
            if kind != "pending" or source == target:
                return True
            if self.sources.get(target, 0) >= 8:
                return False
            self._release(source, kind)
            self.counts[kind] += 1
            self.sources[target] = self.sources.get(target, 0) + 1
            self.requests[connection] = (target, kind, started)
            return True

    def promote(self, connection, kind):
        with self.lock:
            source, previous, started = self.requests[connection]
            if previous == kind:
                return True
            if self.counts[kind] >= self.limits[kind]:
                return False
            self._release(source, previous)
            self.counts[kind] += 1
            self.requests[connection] = (source, kind, started)
            return True

    def _release(self, source, kind):
        self.counts[kind] -= 1
        if kind == "pending":
            self.sources[source] -= 1
            if not self.sources[source]:
                del self.sources[source]

    def leave(self, connection):
        with self.lock:
            record = self.requests.pop(connection, None)
            if record:
                self._release(*record[:2])
