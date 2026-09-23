import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ambergate.config import validate
from ambergate.labels import LabelController, changes, discover, parse_label, reconcile
from ambergate.nginx import digest, render
from ambergate.storage import ConflictError
from .helpers import config


def snapshot(labels=None, port=9000):
    return dict(connected=True, truncated=False, containers=[dict(
        name="backend-1", state="running", is_gateway=False,
        route_labels=labels or {"ambergate.route": f"host=example.com; path=/api; port={port}"},
        endpoints=[dict(address="127.0.0.1", kind="ip")],
        host_endpoints=[dict(address="10.0.0.10", port=19000, container_port=port, kind="published")])])


class LabelTests(unittest.TestCase):
    def test_compact_format_defaults_and_all_options(self):
        basic = parse_label("host=EXAMPLE.com; path=/api; port=3000")
        self.assertEqual(basic["host"], "example.com")
        self.assertEqual(basic["options"]["balance"], "round_robin")
        full = parse_label("host=example.com; path=/v1/api; port=8000; balance=least_conn; strip=true; "
                           "cache=true; ttl=90; websocket=false; timeout=120; body=10; rate=5; burst=8; "
                           "via=host; weight=2; backup=true;")
        self.assertTrue(full["options"]["strip_prefix"])
        self.assertEqual(full["options"]["cache_ttl"], 90)
        self.assertEqual(full["weight"], 2)
        self.assertTrue(full["backup"])
        self.assertEqual(parse_label("group=app-api; port=3000")["group"], "app-api")

    def test_reject_ambiguous_invalid_and_injectable_labels(self):
        for label in ("", "host=example.com", "port=3000", "host=a;host=b;port=1", "host=a;port=0",
                      "host=a;port=65536", "host=a;port=80;strip=yes", "host=a;port=80;via=auto",
                      "host=a;port=80;unknown=1", "host=a;port=80;balance=random", "group=a;host=a;port=80",
                      "group=a;port=80;cache=true", "host=a;path=/api/;port=80", "host=a;port=80;ttl=0",
                      "host=a;port=80;timeout=9999", "host=a;port=80;rate=-1", "host=a;port=80;backup=1",
                      "host=http://a;port=80", "host=a;port=80;path=/api { return 200", "x" * 4097):
            with self.subTest(label=label), self.assertRaises(ValueError):
                parse_label(label)

    def test_named_labels_ports_network_host_and_gateway_exclusion(self):
        data = snapshot({"ambergate.route.api": "host=a.test;path=/api;port=9000",
                         "ambergate.route.front": "host=b.test;port=9000;via=host"})
        entries, errors = discover(data)
        self.assertFalse(errors)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["target"]["port"], 19000)
        data["containers"][0]["is_gateway"] = True
        self.assertEqual(discover(data), ([], []))
        data["containers"][0]["is_gateway"] = False
        data["containers"][0]["state"] = "exited"
        self.assertEqual(discover(data), ([], []))

    def test_network_mode_never_silently_falls_back_to_host(self):
        data = snapshot()
        row = data["containers"][0]
        row["endpoints"] = row["host_endpoints"]
        self.assertTrue(discover(data)[1])
        row["route_labels"]["ambergate.route"] += ";via=host"
        self.assertFalse(discover(data)[1])
        row["host_endpoints"] = []
        self.assertTrue(discover(data)[1])

    def test_balancing_deduplication_idempotence_and_conflicts(self):
        entries, _ = discover(snapshot())
        second = copy.deepcopy(entries[0])
        second["target"]["address"] = "127.0.0.2"
        entries.append(second)
        before = config()
        after = reconcile(before, entries + entries)
        route = after["hosts"][1]["routes"][0]
        self.assertEqual(len(route["docker"]["targets"]), 2)
        self.assertEqual(before["hosts"], after["hosts"][:1])
        self.assertEqual(reconcile(after, list(reversed(entries))), after)
        self.assertEqual(len(changes(before, after)), 1)
        second["spec"]["options"]["balance"] = "least_conn"
        with self.assertRaisesRegex(ValueError, "disagree"):
            reconcile(before, entries)

    def test_group_joins_manual_targets_and_keeps_manual_options(self):
        before = config()
        route = before["hosts"][0]["routes"][0]
        route["docker"] = dict(managed=False, group="app-api", targets=[])
        route["cache"] = True
        entries, _ = discover(snapshot({"ambergate.route": "group=app-api;port=8000;weight=3"}))
        after = reconcile(before, entries)
        updated = after["hosts"][0]["routes"][0]
        self.assertEqual(updated["targets"], route["targets"])
        self.assertTrue(updated["cache"])
        self.assertEqual(updated["docker"]["targets"][0]["weight"], 3)
        self.assertIn("weight=3", render(after, "test"))
        self.assertEqual(reconcile(after, []), before)

    def test_manual_collision_is_rejected(self):
        entries, _ = discover(snapshot({"ambergate.route": "host=gateway.test;port=8000"}))
        with self.assertRaisesRegex(ValueError, "manual route"):
            reconcile(config(), entries)

    def test_zero_replicas_preserve_route_as_503_and_backup_only_is_valid(self):
        entries, _ = discover(snapshot())
        after = reconcile(config(), entries)
        empty = reconcile(after, [])
        text = render(empty, "test")
        self.assertIn('location = /api {\n            set $ambergate_route "/api";\n            return 503;', text)
        self.assertIn("location ^~ /api/", text)
        entries[0]["target"]["backup"] = True
        text = render(reconcile(after, entries), "test")
        self.assertIn("server 127.0.0.1:1 down;", text)
        self.assertIn("backup;", text)

    def test_limits_are_enforced_on_discovered_routes(self):
        entries, _ = discover(snapshot())
        many = []
        for i in range(33):
            entry = copy.deepcopy(entries[0])
            entry["target"]["port"] += i
            many.append(entry)
        with self.assertRaises(ValueError):
            reconcile(config(), many)
        data = snapshot({f"ambergate.route.r{i}": "host=a;port=80" for i in range(17)})
        self.assertTrue(discover(data)[1])
        data = snapshot({"ambergate.route.bad/name": "host=a;port=80"})
        self.assertTrue(discover(data)[1])

    def test_settings_disabled_by_default_persist_and_use_revision_guards(self):
        with tempfile.TemporaryDirectory() as root:
            docker = Mock()
            controller = LabelController(Mock(data=Path(root)), docker)
            state = controller.scan()
            self.assertEqual(state["mode"], "off")
            docker.snapshot.assert_not_called()
            controller.save("preview", state["revision"])
            self.assertEqual(controller.file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(LabelController(Mock(data=Path(root)), docker).policy(), {"mode": "preview"})
            with self.assertRaises(ConflictError):
                controller.save("auto", state["revision"])
            with self.assertRaises(ValueError):
                controller.save("yes", digest(controller.policy()))
