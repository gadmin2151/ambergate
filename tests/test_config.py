import unittest

from ambergate.config import ValidationError, default_config, validate
from ambergate.nginx import render
from .helpers import config, route


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_complete_config(self):
        self.assertEqual(validate(default_config()), default_config())
        self.assertEqual(validate(config()), config())

    def test_reject_directive_injection(self):
        cases = [lambda c: c["hosts"][0].update(domain="x; include /etc/passwd;"),
                 lambda c: c["hosts"][0]["routes"][0].update(path='/x" { return 200; }'),
                 lambda c: c["hosts"][0]["routes"][0]["targets"][0].update(address="x\nserver evil;"),
                 lambda c: c["settings"].update(resolvers=["fe80::1%eth;\ninclude /etc/passwd;"]),
                 lambda c: c["settings"].update(trusted_proxies=["fe80::1%eth;\ninclude /etc/passwd;/128"]),
                 lambda c: c["hosts"][0]["routes"][0].update(id="x;"),
                 lambda c: c["settings"].update(rate_rps=True),
                 lambda c: c["settings"].update(extra="return 200;")]
        for mutate in cases:
            c = config()
            mutate(c)
            with self.subTest(config=c), self.assertRaises(ValidationError):
                validate(c)

    def test_path_collisions_and_invalid_paths(self):
        for path in ["/api/", "/api?query", "/api.foo", "api", "/api//v1"]:
            c = config()
            c["hosts"][0]["routes"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(ValidationError):
                validate(c)
        c = config()
        c["hosts"][0]["routes"].append(route(id="other"))
        with self.assertRaises(ValidationError):
            validate(c)

    def test_backup_constraints(self):
        c = config()
        r = c["hosts"][0]["routes"][0]
        r["targets"][0]["backup"] = True
        with self.assertRaises(ValidationError):
            validate(c)
        r["targets"].append({"address": "backend", "port": 80, "weight": 1, "backup": False})
        validate(c)
        r["balance"] = "ip_hash"
        with self.assertRaises(ValidationError):
            validate(c)

    def test_prefix_matching_is_bounded(self):
        c = config()
        c["hosts"][0]["routes"].append(route("/api", id="api"))
        text = render(c, "test")
        self.assertIn("location = /api {", text)
        self.assertIn("location ^~ /api/ {", text)
        self.assertNotIn("location /api {", text)

    def test_hostnames_are_resolved_at_runtime(self):
        c = config()
        c["hosts"][0]["routes"][0]["targets"][0]["address"] = "backend-1"
        self.assertIn("fail_timeout=10s resolve;", render(c, "test"))

    def test_docker_names_support_underscores_only_for_upstream_addresses(self):
        c = config()
        c["hosts"][0]["routes"][0]["targets"][0]["address"] = "project_backend_1"
        self.assertIn("server project_backend_1:9000", render(c, "test"))
        c["hosts"][0]["domain"] = "bad_domain.test"
        with self.assertRaises(ValidationError):
            validate(c)

    def test_cache_namespace_changes_with_destination(self):
        c = config()
        c["hosts"][0]["routes"][0]["cache"] = True
        before = [line for line in render(c, "a").splitlines() if "proxy_cache_key" in line]
        c["hosts"][0]["routes"][0]["targets"][0]["port"] = 9999
        after = [line for line in render(c, "b").splitlines() if "proxy_cache_key" in line]
        self.assertNotEqual(before, after)

    def test_untrusted_forwarding_and_cache_safety(self):
        c = config()
        c["hosts"][0]["routes"][0]["cache"] = True
        text = render(c, "test")
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", text)
        self.assertNotIn("set_real_ip_from", text)
        self.assertIn("$signed_request $has_upgrade", text)
        self.assertIn("$upstream_http_set_cookie", text)


if __name__ == "__main__":
    unittest.main()
