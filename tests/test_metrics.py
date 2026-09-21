import json
import unittest

from gateway.metrics import MAX_DOMAINS, Metrics


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.metrics = Metrics("/tmp/unused-gateway-metrics")
        self.metrics.started = 1000

    def record(self, **overrides):
        value = dict(time=1001, host="example.com", route="/api", status=200,
                     bytes=100, seconds=.02, cache="MISS")
        value.update(overrides)
        self.metrics.record(b"<190>Jan 01 00:00:01 gateway: " + json.dumps(value).encode(), now=value["time"])

    def test_status_latency_bytes_and_cache_are_actual_observations(self):
        self.record(cache="HIT", seconds=.01)
        self.record(status=429, seconds=.02, cache="BYPASS", limit="REJECTED/-")
        self.record(status=502, seconds=.03, cache="-")
        self.record(status=301, seconds=.04, bytes=250)
        data = self.metrics.snapshot(now=1011)
        total = data["summary"]
        self.assertEqual(total["requests"], 4)
        self.assertEqual(total["statuses"], [0, 1, 1, 1, 1])
        self.assertEqual(total["errors"], 1)
        self.assertEqual(total["error_percent"], 25)
        self.assertEqual(total["limited"], 1)
        self.assertEqual(total["bytes"], 550)
        self.assertEqual(total["avg_ms"], 25)
        self.assertEqual(total["p95_ms"], 40)
        self.assertEqual(total["cache_hit_percent"], 50)
        self.assertEqual(data["rps"], .4)
        self.assertEqual([e["status"] for e in data["events"]], [502, 429])
        self.assertEqual(data["domains"]["example.com"], total)

    def test_no_observations_are_not_zero_latency_or_perfect_cache(self):
        data = self.metrics.snapshot(now=1005)
        self.assertEqual(data["summary"]["requests"], 0)
        for field in ("avg_ms", "p95_ms", "error_percent", "cache_hit_percent"):
            self.assertIsNone(data["summary"][field])
        self.assertIsNone(data["rps"])
        self.assertEqual(data["events"], [])

    def test_rollover_expires_old_counts_and_events(self):
        self.record(status=502)
        self.record(time=1902, host="next.test", status=404)
        data = self.metrics.snapshot(now=1911)
        self.assertEqual(data["summary"]["requests"], 1)
        self.assertEqual(list(data["domains"]), ["next.test"])
        self.assertEqual(len(data["events"]), 1)
        self.assertLessEqual(len(data["series"]), 91)
        self.assertEqual(self.metrics.snapshot(now=2900)["summary"]["requests"], 0)

    def test_cardinality_and_recent_events_remain_bounded(self):
        for i in range(400):
            self.record(host=f"host-{i}.test", status=404)
        data = self.metrics.snapshot(now=1010)
        self.assertLessEqual(len(data["domains"]), MAX_DOMAINS + 1)
        self.assertEqual(len(data["events"]), 30)
        self.assertEqual(data["summary"]["requests"], 400)
        self.assertEqual(sum(s["requests"] for s in data["domains"].values()), 400)

    def test_malformed_datagrams_cannot_poison_telemetry(self):
        for packet in (b"garbage", b"{}", b"{broken", b'{"time":"NaN"}', b"[]"):
            self.metrics.record(packet, now=1001)
        for field, value in (("seconds", float("nan")), ("seconds", float("inf")),
                             ("status", 999), ("host", "a" * 254), ("route", ["/secret"]),
                             ("bytes", -1), ("cache", {})):
            self.record(**{field: value})
        self.assertEqual(self.metrics.snapshot(now=1005)["summary"]["requests"], 0)

    def test_partial_start_interval_is_not_presented_as_full_rate(self):
        self.metrics.started = 1009
        self.record(time=1009)
        self.assertIsNone(self.metrics.snapshot(now=1010)["rps"])

    def test_upstream_429_is_not_counted_as_gateway_limit(self):
        self.record(status=429, limit="PASSED/PASSED")
        self.record(status=429, limit="-/REJECTED")
        data = self.metrics.snapshot(now=1005)
        self.assertEqual(data["summary"]["statuses"][3], 2)
        self.assertEqual(data["summary"]["limited"], 1)
