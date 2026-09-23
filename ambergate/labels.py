"""Bounded, read-only Docker label discovery and transactional Nginx updates."""
import copy
import json
import re
import threading
import time

from .config import default_config, hostname, integer, validate
from .nginx import digest
from .storage import ConflictError, write_json


PREFIX = "ambergate.route"
DEFAULTS = dict(strip_prefix=False, balance="round_robin", cache=False, cache_ttl=60,
                websocket=True, timeout=60, body_mb=0, rate_rps=0, rate_burst=0)
OPTIONS = dict(strip="strip_prefix", balance="balance", cache="cache", ttl="cache_ttl",
               websocket="websocket", timeout="timeout", body="body_mb", rate="rate_rps", burst="rate_burst")


def parse_label(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError("Label must contain 1–4096 characters")
    fields = {}
    for item in value.split(";"):
        if not item.strip():
            continue
        key, sep, val = item.strip().partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not val or key in fields:
            raise ValueError("Use unique key=value fields separated by semicolons")
        if key not in {*OPTIONS, "host", "path", "port", "group", "via", "weight", "backup"}:
            raise ValueError(f"Unknown label option: {key}")
        fields[key] = val
    if "port" not in fields:
        raise ValueError("port is required")
    group = fields.get("group", "")
    if group:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", group):
            raise ValueError("group must contain 1–64 letters, digits, _ or -")
        if set(fields) - {"group", "port", "via", "weight", "backup"}:
            raise ValueError("group uses the existing route settings; omit host, path and route options")
    else:
        if "host" not in fields:
            raise ValueError("host or group is required")
        hostname(fields["host"], "host")
    via = fields.get("via", "network")
    if via not in ("network", "host"):
        raise ValueError("via must be network or host")
    values = dict(DEFAULTS)
    for key, dest in OPTIONS.items():
        if key not in fields:
            continue
        raw = fields[key]
        if type(values[dest]) is bool:
            if raw not in ("true", "false"):
                raise ValueError(f"{key} must be true or false")
            values[dest] = raw == "true"
        elif type(values[dest]) is int:
            if not re.fullmatch(r"[0-9]{1,7}", raw):
                raise ValueError(f"{key} must be an integer")
            values[dest] = int(raw)
        else:
            values[dest] = raw
    for key, default, high in (("port", "", 65535), ("weight", "1", 1000)):
        raw = fields.get(key, default)
        if not re.fullmatch(r"[0-9]{1,5}", raw):
            raise ValueError(f"{key} must be an integer")
        fields[key] = int(raw)
        integer(fields[key], 1, high, key)
    backup = fields.get("backup", "false")
    if backup not in ("true", "false"):
        raise ValueError("backup must be true or false")
    spec = dict(host=fields.get("host", "").lower(), path=fields.get("path", "/"), group=group,
                port=fields["port"], weight=fields["weight"], backup=backup == "true", via=via, options=values)
    # Use exactly the same path, range and algorithm rules as the route editor.
    sample = default_config()
    sample["hosts"] = [dict(id="h", domain=spec["host"] or "example.com", enabled=True,
                           routes=[new_route(spec, "r")])]
    validate(sample)
    return spec


def new_route(spec, identity):
    return dict(id=identity, name="Docker " + spec["path"][:73], path=spec["path"], targets=[],
                docker=dict(managed=True, group="", targets=[]), **spec["options"])


def discover(snapshot):
    entries, errors = [], []
    for container in snapshot["containers"]:
        if container["state"] != "running" or container.get("is_gateway"):
            continue
        labels = container.get("route_labels", {})
        if len(labels) > 16:
            errors.append(f"{container['name']}: at most 16 route labels per container")
            continue
        for key, value in sorted(labels.items()):
            try:
                if key != PREFIX and not re.fullmatch(r"ambergate\.route\.[a-zA-Z0-9_-]{1,64}", key):
                    raise ValueError("Invalid route label name")
                spec = parse_label(value)
                if spec["via"] == "host":
                    endpoints = [e for e in container["host_endpoints"] if e["container_port"] == spec["port"]]
                else:
                    endpoints = [e for e in container["endpoints"] if e["kind"] in ("dns", "ip")]
                if not endpoints:
                    raise ValueError("No reachable endpoint: use a shared Docker network, or via=host with a published port and Docker host IP")
                endpoint = endpoints[0]
                target = dict(address=endpoint["address"], port=endpoint.get("port", spec["port"]),
                              weight=spec["weight"], backup=spec["backup"])
                entries.append(dict(spec=spec, target=target, container=container["name"]))
            except ValueError as exc:
                errors.append(f"{container['name']} / {key}: {exc}")
    return entries, errors[:100]


def reconcile(config, entries, frozen_sources=()):
    """Only Docker-owned routes/targets change; empty routes remain as 503 guards."""
    result = copy.deepcopy(config)
    hosts = {h["domain"].lower(): h for h in result["hosts"]}
    groups, options, ownership = {}, {}, {}
    for host in result["hosts"]:
        for route in host["routes"]:
            if "docker" in route:
                sources = route["docker"].get("sources", {"local": route["docker"]["targets"]})
                ownership[route["id"]] = {k: [copy.deepcopy(t) for t in v if t not in route["targets"]]
                                          for k, v in sources.items() if k in frozen_sources}
                route["docker"]["targets"] = []
                for targets in ownership[route["id"]].values():
                    for target in targets:
                        if target not in route["docker"]["targets"] and target not in route["targets"]:
                            route["docker"]["targets"].append(target)
                if route["docker"]["managed"] and route["docker"]["targets"]:
                    options[host["domain"].lower(), route["path"]] = {k: route[k] for k in DEFAULTS}
                if not route["docker"]["managed"]:
                    groups.setdefault(route["docker"]["group"], []).append(route)
    for entry in entries:
        spec, target = entry["spec"], entry["target"]
        if spec["group"]:
            destinations = groups.get(spec["group"], [])
        else:
            domain, path = spec["host"], spec["path"]
            key = domain, path
            if key in options and options[key] != spec["options"]:
                raise ValueError(f"{domain}{path}: containers disagree on route options")
            options[key] = spec["options"]
            if domain not in hosts:
                host = dict(id="docker_host_" + digest(domain), domain=domain, enabled=True, routes=[])
                hosts[domain] = host
                result["hosts"].append(host)
            host = hosts[domain]
            route = next((r for r in host["routes"] if r["path"] == path), None)
            if route is None:
                route = new_route(spec, "docker_route_" + digest(key))
                host["routes"].append(route)
            elif not route.get("docker", {}).get("managed"):
                raise ValueError(f"{domain}{path}: manual route already exists; use group instead")
            route.update(spec["options"])
            destinations = [route]
        for route in destinations:
            targets = route["docker"]["targets"]
            existing = next((t for t in route["targets"] + targets
                             if (t["address"], t["port"]) == (target["address"], target["port"])), None)
            if existing and existing != target:
                raise ValueError("Conflicting weight/backup for the same upstream endpoint")
            if not existing:
                targets.append(copy.deepcopy(target))
            if target not in route["targets"]:
                members = ownership.setdefault(route["id"], {}).setdefault(entry.get("source", "local"), [])
                if target not in members:
                    members.append(copy.deepcopy(target))
    for host in result["hosts"]:
        for route in host["routes"]:
            if "docker" in route:
                route["docker"]["targets"].sort(key=lambda t: (t["address"], t["port"]))
                sources = ownership.get(route["id"], {})
                if "sources" in route["docker"] or any(k != "local" for k in sources):
                    route["docker"]["sources"] = {k: sorted(v, key=lambda t: (t["address"], t["port"]))
                                                 for k, v in sorted(sources.items())}
    return validate(result)


def changes(before, after):
    old = {r["id"]: r for h in before["hosts"] for r in h["routes"]}
    return [dict(host=h["domain"], path=r["path"], group=r["docker"]["group"],
                 action="update" if r["id"] in old else "create", targets=r["docker"]["targets"])
            for h in after["hosts"] for r in h["routes"] if "docker" in r and r != old.get(r["id"])]


class LabelController:
    interval = 5

    def __init__(self, store, docker, agents=None):
        self.store, self.docker, self.agents = store, docker, agents
        self.file = store.data / "labels.json"
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = None
        self.state = dict(checked_at=None, errors=[], warnings=[], changes=[], token=None, applied_at=None)

    def policy(self):
        value = json.loads(self.file.read_text()) if self.file.exists() else {"mode": "off"}
        if not isinstance(value, dict) or set(value) != {"mode"} or value["mode"] not in ("off", "preview", "auto"):
            raise ValueError("Label mode must be off, preview or auto")
        return value

    def snapshot(self):
        policy = self.policy()
        return {**copy.deepcopy(self.state), **policy, "revision": digest(policy), "interval": self.interval}

    def save(self, mode, revision):
        with self.lock:
            if revision != digest(self.policy()):
                raise ConflictError("Label settings changed. Refresh and try again.")
            if mode not in ("off", "preview", "auto"):
                raise ValueError("Label mode must be off, preview or auto")
            write_json(self.file, {"mode": mode})
            self.state = {**self.state, "changes": [], "errors": [], "warnings": [], "token": None}
            return self.snapshot()

    def scan(self, apply=False, token=None):
        with self.lock:
            mode = self.policy()["mode"]
            if mode == "off":
                return self.snapshot()
            snapshot = self.docker.snapshot(force=True)
            state = dict(checked_at=time.time(), errors=[], warnings=[], changes=[], token=None,
                         applied_at=self.state["applied_at"])
            try:
                remote, warnings, configured = self.agents.routing() if self.agents else ([], [], False)
                frozen = ()
                if not snapshot["connected"] and not configured:
                    raise ValueError("Docker unavailable; routes kept unchanged")
                if snapshot["truncated"] and not configured:
                    raise ValueError("Docker inventory is incomplete (500 containers); routes kept unchanged")
                if not snapshot["connected"] or snapshot["truncated"]:
                    entries, frozen = [], ("local",)
                    if snapshot.get("settings", {}).get("enabled"):
                        warnings.append("Local Docker unavailable; local targets kept unchanged")
                else:
                    entries, state["errors"] = discover(snapshot)
                if state["errors"]:
                    self.state = state
                    return self.snapshot()
                entries += remote
                with self.store.lock:
                    active, draft = self.store.active_config(), self.store.draft_config()
                    groups = {r.get("docker", {}).get("group") for c in (active, draft)
                              for h in c["hosts"] for r in h["routes"]}
                    state["warnings"] = sorted(set(warnings) | {f"No route uses group={e['spec']['group']}"
                        for e in entries if e["spec"]["group"] and e["spec"]["group"] not in groups})[:100]
                    candidate, next_draft = reconcile(active, entries, frozen), reconcile(draft, entries, frozen)
                    state["changes"] = changes(active, candidate)
                    state["token"] = digest([active, draft, candidate, next_draft])
                    if token is not None and token != state["token"]:
                        raise ConflictError("Label preview changed. Review the new preview and try again.")
                    if apply and (active != candidate or draft != next_draft):
                        # Active settings and the saved draft are reconciled separately.
                        # Saving first gives a recoverable pending draft after a crash;
                        # failed validation/reload restores it exactly.
                        write_json(self.store.draft, next_draft)
                        try:
                            if active != candidate:
                                self.store.apply_config(candidate)
                        except Exception:
                            write_json(self.store.draft, draft)
                            raise
                        state["applied_at"] = time.time()
                        state["changes"] = []
                        state["token"] = None
            except ConflictError:
                self.state = state
                raise
            except Exception as exc:
                state["errors"] = [str(exc)]
                state["token"] = None
            self.state = state
            return self.snapshot()

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, name="ambergate-labels", daemon=True)
            self.thread.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                with self.lock:
                    self.scan(apply=self.policy()["mode"] == "auto")
            except Exception as exc:
                self.state = {**self.state, "errors": [str(exc)], "token": None}
            self.stopping.wait(self.interval)

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=25)
