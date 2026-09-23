"""Bounded, in-memory traffic telemetry received from Nginx over a local socket.

Only completed requests are counted. No URLs, queries, IPs or headers are
collected. Datagram delivery is best effort: this is an operational overview,
not an accounting or audit log. Nothing is written to disk.
"""
from bisect import bisect_left
from collections import deque
import json
import math
import os
from pathlib import Path
import socket
import threading
import time


STEP = 10
WINDOW = 900
MAX_DOMAINS = 128
LATENCIES = (.005, .01, .025, .05, .1, .25, .5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 3600)
CACHE_LOOKUPS = {"HIT", "MISS", "EXPIRED", "STALE", "UPDATING", "REVALIDATED"}


class Counter:
    def __init__(self):
        self.requests = self.bytes = self.hits = self.lookups = self.limited = 0
        self.seconds = self.maximum = 0
        self.statuses = [0] * 5
        self.histogram = [0] * (len(LATENCIES) + 1)

    def add(self, status, size, seconds, cache, limited):
        self.requests += 1
        self.bytes += size
        self.seconds += seconds
        self.maximum = max(self.maximum, seconds)
        self.statuses[status // 100 - 1] += 1
        self.limited += limited
        self.hits += cache == "HIT"
        self.lookups += cache in CACHE_LOOKUPS
        self.histogram[bisect_left(LATENCIES, seconds)] += 1

    def merge(self, other):
        for key in ("requests", "bytes", "seconds", "hits", "lookups", "limited"):
            setattr(self, key, getattr(self, key) + getattr(other, key))
        self.maximum = max(self.maximum, other.maximum)
        self.statuses = [a + b for a, b in zip(self.statuses, other.statuses)]
        self.histogram = [a + b for a, b in zip(self.histogram, other.histogram)]

    def result(self):
        p95 = None
        if self.requests:
            cumulative = 0
            for index, count in enumerate(self.histogram):
                cumulative += count
                if cumulative >= math.ceil(self.requests * .95):
                    p95 = min(LATENCIES[index], self.maximum) if index < len(LATENCIES) else self.maximum
                    break
        return dict(requests=self.requests, bytes=self.bytes, errors=self.statuses[4],
                    statuses=self.statuses, limited=self.limited, cache_hits=self.hits,
                    cache_lookups=self.lookups,
                    cache_hit_percent=round(100 * self.hits / self.lookups, 1) if self.lookups else None,
                    error_percent=round(100 * self.statuses[4] / self.requests, 2) if self.requests else None,
                    avg_ms=round(1000 * self.seconds / self.requests, 1) if self.requests else None,
                    p95_ms=round(p95 * 1000, 1) if p95 is not None else None)


class Metrics:
    def __init__(self, run_dir):
        self.path = Path(run_dir) / "metrics.sock"
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = self.socket = None
        self.started = time.time()
        self.buckets = {}
        self.events = deque(maxlen=30)
        self.last_request = None

    def start(self):
        self.path.unlink(missing_ok=True)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self.socket.bind(str(self.path))
            # The Nginx master and workers may have different UIDs.
            os.chmod(self.path, 0o666)
            self.socket.settimeout(.25)
        except Exception:
            self.socket.close()
            self.path.unlink(missing_ok=True)
            raise
        with self.lock:
            self.started = time.time()
            self.buckets.clear()
            self.events.clear()
            self.last_request = None
        self.stopping.clear()
        self.thread = threading.Thread(target=self.receive, name="gateway-metrics", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.socket:
            self.socket.close()
        self.path.unlink(missing_ok=True)

    def receive(self):
        while not self.stopping.is_set():
            try:
                packet = self.socket.recv(4096)
                self.record(packet)
            except socket.timeout:
                continue
            except OSError:
                break

    def prune(self, now):
        # At most 90 ten-second buckets, including the current partial bucket.
        cutoff = (int(now) // STEP - WINDOW // STEP + 1) * STEP
        for key in list(self.buckets):
            if key < cutoff:
                del self.buckets[key]
        while self.events and self.events[0]["time"] < cutoff:
            self.events.popleft()
        return cutoff

    def record(self, packet, now=None):
        now = time.time() if now is None else now
        try:
            value = json.loads(packet[packet.index(b"{"):])
            stamp, seconds = float(value["time"]), float(value["seconds"])
            status, size = int(value["status"]), int(value["bytes"])
            host, route, cache = value["host"], value["route"], value["cache"]
            limit = value.get("limit", "-/-")
            if (not math.isfinite(stamp) or not math.isfinite(seconds) or not 0 <= seconds <= 31536000
                    or not 100 <= status <= 599 or size < 0 or size > 2**63
                    or not isinstance(host, str) or not 0 < len(host) <= 253
                    or not isinstance(route, str) or len(route) > 200
                    or not isinstance(cache, str) or len(cache) > 20
                    or not isinstance(limit, str) or len(limit) > 50
                    or stamp < now - WINDOW or stamp > now + STEP):
                return
        except (ValueError, KeyError, TypeError, OverflowError):
            return
        with self.lock:
            self.prune(now)
            key = int(stamp) // STEP * STEP
            bucket = self.buckets.setdefault(key, {})
            # Names come from $server_name, never from arbitrary Host headers.
            # Bound cardinality even during rapid configuration changes.
            if host not in bucket and len(bucket) >= MAX_DOMAINS:
                host = "__other__"
            limited = "REJECTED" in limit.split("/")
            bucket.setdefault(host, Counter()).add(status, size, seconds, cache, limited)
            self.last_request = max(stamp, self.last_request or stamp)
            if status >= 400:
                self.events.append(dict(time=stamp, host=host, route=route, status=status,
                                        ms=round(seconds * 1000, 1), limited=limited))

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            cutoff = self.prune(now)
            total, domains, series = Counter(), {}, []
            first = max(cutoff, int(self.started) // STEP * STEP)
            for stamp in range(first, int(now) // STEP * STEP + 1, STEP):
                point = Counter()
                for host, counter in self.buckets.get(stamp, {}).items():
                    point.merge(counter)
                    # Also bound the whole-window aggregate during domain churn.
                    name = host if host in domains or len(domains) < MAX_DOMAINS else "__other__"
                    domains.setdefault(name, Counter()).merge(counter)
                total.merge(point)
                series.append(dict(time=stamp, requests=point.requests, errors=point.statuses[4]))
            # Rate uses ten completed seconds, never a partially filled chart bucket.
            recent = [p for p in series if now - 20 < p["time"] <= now - 10 and p["time"] >= self.started]
            rps = sum(p["requests"] for p in recent) / STEP if recent else None
            return dict(collected_since=self.started, updated_at=now, window_seconds=WINDOW,
                        bucket_seconds=STEP, available=self.thread is not None and self.thread.is_alive(),
                        last_request_at=self.last_request, rps=rps, summary=total.result(),
                        series=series, domains={host: c.result() for host, c in domains.items()},
                        events=list(reversed(self.events)))
