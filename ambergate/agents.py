"""Agent identities, bounded inventory and source-scoped routing leases."""
import copy
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time

from .config import boolean, integer, obj, sequence, upstream_hostname
from .labels import parse_label
from .nginx import digest
from .storage import ConflictError, write_json

TOKEN = re.compile(r"ag_([a-f0-9]{32})\.([A-Za-z0-9_-]{43})")
MANUAL_TARGET = re.compile(r"docker:([a-f0-9]{64}):([1-9][0-9]{0,4})")


class AgentUnauthorized(ValueError):
    pass


def text(value, size, field):
    if not isinstance(value, str) or len(value) > size or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid agent field: {field}")
    return value


def inventory(raw):
    obj(raw, ("version", "connected", "truncated", "engine_version", "message", "containers"), "agent report")
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("Unsupported agent protocol version")
    for name in ("connected", "truncated"):
        boolean(raw[name], name)
    text(raw["engine_version"], 80, "engine_version")
    text(raw["message"], 512, "message")
    sequence(raw["containers"], 0, 500, "containers")
    ids, names = set(), set()
    for c in raw["containers"]:
        obj(c, ("id", "name", "image", "state", "status", "project", "service", "networks", "shared_networks",
                "ports", "endpoints", "route_labels", "is_gateway"), "agent container")
        if not isinstance(c["id"], str) or not re.fullmatch(r"[a-f0-9]{64}", c["id"]) or c["id"] in ids:
            raise ValueError("Invalid or duplicate container ID")
        ids.add(c["id"])
        upstream_hostname(c["name"], "container name")
        if c["name"] in names:
            raise ValueError("Duplicate container name")
        names.add(c["name"])
        for field, size in (("image", 300), ("state", 30), ("status", 150), ("project", 100), ("service", 100)):
            text(c[field], size, field)
        boolean(c["is_gateway"], "is_gateway")
        for field in ("networks", "shared_networks"):
            sequence(c[field], 0, 32, field)
            for network in c[field]:
                text(network, 128, "network")
        sequence(c["ports"], 0, 128, "ports")
        for port in c["ports"]:
            integer(port, 1, 65535, "port")
        sequence(c["endpoints"], 0, 32, "endpoints")
        for endpoint in c["endpoints"]:
            obj(endpoint, ("address", "kind"), "endpoint")
            if endpoint["kind"] == "ip":
                ipaddress.ip_address(endpoint["address"])
                if "%" in endpoint["address"]:
                    raise ValueError("IPv6 zone IDs are unsupported")
            elif endpoint["kind"] == "dns":
                upstream_hostname(endpoint["address"], "endpoint")
            elif endpoint["kind"] == "namespace":
                if endpoint["address"] != c["id"] or c["is_gateway"] or c["state"] != "running":
                    raise ValueError("Invalid namespace endpoint")
            else:
                raise ValueError("Agent endpoints must belong to a shared Docker network")
        if not isinstance(c["route_labels"], dict) or len(c["route_labels"]) > 16:
            raise ValueError("At most 16 route labels per container")
        for key, value in c["route_labels"].items():
            if key != "ambergate.route" and not re.fullmatch(r"ambergate\.route\.[a-zA-Z0-9_-]{1,64}", key):
                raise ValueError("Invalid route label name")
            if not isinstance(value, str) or not 1 <= len(value) <= 4096:
                raise ValueError("Label must contain 1–4096 characters")
    return copy.deepcopy(raw)


def reachable_container(row):
    return bool(row["endpoints"] and (row["shared_networks"] or row["endpoints"][0]["kind"] == "namespace"))


def wire_snapshot(snapshot, namespace=False):
    """Allow-list metadata; never send Docker env, mounts or unrelated labels."""
    rows = []
    for row in snapshot["containers"]:
        clean = {key: row[key] for key in ("id", "name", "image", "state", "status", "project", "service",
                 "networks", "shared_networks", "ports", "route_labels", "is_gateway")}
        # Protocol v1 keeps the shared_networks key for reachable private
        # networks, including host-mode bridges. Older centers remain compatible.
        clean["shared_networks"] = row.get("reachable_networks", row["shared_networks"])
        clean["endpoints"] = [{"address": e["address"], "kind": e["kind"]} for e in row["endpoints"]
                              if e["kind"] in ("dns", "ip")]
        if namespace and row["state"] == "running" and not row["is_gateway"]:
            clean["endpoints"] = [dict(address=row["id"], kind="namespace")]
        rows.append(clean)
    return inventory(dict(version=1, connected=snapshot["connected"], truncated=snapshot["truncated"],
                          engine_version=snapshot["engine_version"] or "", message=snapshot["message"][:512], containers=rows))


def target_id(container_id, port):
    return hashlib.sha256(f"{container_id}:{port}".encode()).hexdigest()[:32]


def agent_routes(snapshot):
    entries, errors = [], []
    for row in snapshot["containers"]:
        if row["state"] != "running" or row["is_gateway"]:
            continue
        for key, value in sorted(row["route_labels"].items()):
            try:
                spec = parse_label(value)
                if spec["via"] != "network":
                    raise ValueError("Agent tunnel routes use private container ports; omit via or use via=network")
                if not row["endpoints"]:
                    raise ValueError("Use host networking on Linux or connect the agent to the application network")
                entries.append(dict(spec=spec, container=row["name"], address=row["endpoints"][0]["address"],
                                    target_id=target_id(row["id"], spec["port"])))
            except ValueError as exc:
                errors.append(f"{row['name']} / {key}: {exc}")
    return entries, errors[:100]


class Agents:
    def __init__(self, data, hub, clock=time.time):
        self.data, self.hub, self.clock = data, hub, clock
        self.file = data / "agents.json"
        self.manual_file = data / "agent-manual.json"
        self.manual = json.loads(self.manual_file.read_text()) if self.manual_file.exists() else []
        if not isinstance(self.manual, list) or len(self.manual) > 1024:
            raise ValueError("Invalid manual agent targets")
        for target in self.manual:
            obj(target, ("id", "container", "port"), "manual agent target")
            if not isinstance(target["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", target["id"]):
                raise ValueError("Invalid manual agent ID")
            upstream_hostname(target["container"], "container")
            integer(target["port"], 1, 65535, "port")
        self.directory = data / "agents"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.definitions = json.loads(self.file.read_text()) if self.file.exists() else []
        self.reports, self.rate = {}, {}
        for agent in self.definitions:
            path = self.directory / (agent["id"] + ".json")
            if path.exists():
                report = json.loads(path.read_text())
                if "routes" not in report:
                    # Old preview-only reservations may have been pruned at startup.
                    # Reconstruct logical targets, never reuse a now-unowned numeric port.
                    routes = []
                    for entry in report.get("entries", []):
                        key = self.hub.key(agent["id"], entry["container"], entry["spec"]["port"])
                        destination = report.get("destinations", {}).get(key)
                        if destination:
                            routes.append(dict(spec=entry["spec"], container=entry["container"], target_id=destination))
                    report["routes"] = routes
                    report.pop("entries", None)
                    report.pop("destinations", None)
                self.reports[agent["id"]] = report

    def revision(self):
        return digest(self.definitions)

    def find(self, identity):
        agent = next((a for a in self.definitions if a["id"] == identity), None)
        if agent is None:
            raise ValueError("Agent not found")
        return agent

    def authenticate(self, authorization):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        match = TOKEN.fullmatch(token)
        with self.lock:
            agent = next((a for a in self.definitions if match and a["id"] == match[1]), None)
            if not agent or not agent["enabled"] or not hmac.compare_digest(agent["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
                raise AgentUnauthorized("Invalid or revoked agent token")
            return agent["id"]

    def mutate(self, body):
        with self.lock:
            if body.get("revision") != self.revision():
                raise ConflictError("Agent settings changed. Refresh and try again.")
            action, token = body.get("action"), None
            before = copy.deepcopy(self.definitions)
            if action == "create":
                if len(self.definitions) >= 32:
                    raise ValueError("At most 32 agents are supported")
                agent = dict(id=secrets.token_hex(16), name="", timeout=30, enabled=True, created_at=self.clock())
                self.definitions.append(agent)
            else:
                agent = self.find(body.get("id"))
            try:
                if action in ("create", "update"):
                    name = text(body.get("name"), 80, "name").strip()
                    if not name:
                        raise ValueError("Agent name is required")
                    integer(body.get("timeout", 30), 15, 300, "agent timeout")
                    boolean(body.get("enabled", True), "enabled")
                    agent.update(name=name, timeout=body.get("timeout", 30), enabled=body.get("enabled", True))
                if action in ("create", "rotate"):
                    token = "ag_" + agent["id"] + "." + secrets.token_urlsafe(32)
                    agent["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
                elif action == "delete":
                    self.definitions.remove(agent)
                elif action != "update":
                    raise ValueError("Unknown agent action")
                write_json(self.file, self.definitions)
            except BaseException:
                self.definitions = before
                raise
            if action in ("rotate", "delete") or not agent["enabled"]:
                self.hub.disconnect(agent["id"])
                self.hub.targets(agent["id"], {})
                self.reports.pop(agent["id"], None)
                self.rate.pop(agent["id"], None)
                (self.directory / (agent["id"] + ".json")).unlink(missing_ok=True)
            result = self.snapshot()
            if token:
                result.update(token=token, agent_id=agent["id"])
            return result

    def select_targets(self, body):
        """Reserve stable loopback endpoints; route edits still require Save / Apply."""
        obj(body, ("agent_id", "targets"), "agent targets")
        sequence(body["targets"], 1, 32, "agent targets")
        with self.lock:
            agent = self.find(body["agent_id"])
            report = self.reports.get(agent["id"], {})
            if (not agent["enabled"] or not self.hub.connected(agent["id"])
                    or not 0 <= self.clock() - report.get("seen_at", 0) < agent["timeout"]
                    or not report.get("inventory", {}).get("connected")
                    or report["inventory"]["truncated"]):
                raise ValueError("Agent is unavailable. Refresh its connection and try again.")
            if not report.get("manual_routing"):
                raise ValueError("Update the agent image to select containers without labels.")
            selected = []
            for target in body["targets"]:
                obj(target, ("container_id", "port"), "agent target")
                integer(target["port"], 1, 65535, "container port")
                row = next((c for c in report["inventory"]["containers"] if c["id"] == target["container_id"]), None)
                if not row or row["state"] != "running" or row["is_gateway"] or not reachable_container(row):
                    raise ValueError("Container is unavailable or has no shared network with its agent.")
                value = dict(id=agent["id"], container=row["name"], port=target["port"])
                if value not in selected:
                    selected.append(value)
            additions = [t for t in selected if t not in self.manual]
            if len(self.manual) + len(additions) > 1024:
                raise ValueError("Agent tunnel target limit reached (1024)")
            result = []
            def retained():
                saved = json.loads(self.manual_file.read_text()) if self.manual_file.exists() else []
                return {self.hub.lookup(t["id"], t["container"], t["port"])[1] for t in saved}
            with self.hub.reserve([(t["id"], t["container"], t["port"]) for t in selected], retain=retained) as ports:
                for target in selected:
                    port = ports[self.hub.key(target["id"], target["container"], target["port"])]
                    result.append(dict(address="127.0.0.1", port=port, weight=1, backup=False, agent=target))
                if additions:
                    write_json(self.manual_file, self.manual + additions)
                    self.manual += additions
                self.refresh_targets(agent, report)
            return dict(targets=result)

    def refresh_targets(self, agent, report):
        now = self.clock()
        destinations = {}
        if 0 <= now - report.get("valid_at", 0) < agent["timeout"]:
            for route in report.get("routes", []):
                key, port = self.hub.lookup(agent["id"], route["container"], route["spec"]["port"])
                if port is not None:
                    destinations[key] = route["target_id"]
            # Upgrade compatibility: old reports already contain allocated destinations.
            if "routes" not in report:
                destinations.update(report.get("destinations", {}))
        raw = report.get("inventory", {})
        manual_live = (agent["enabled"] and report.get("manual_routing") and raw.get("connected")
                       and not raw.get("truncated") and 0 <= now - report.get("seen_at", 0) < agent["timeout"])
        if manual_live:
            rows = {c["name"]: c for c in raw["containers"] if c["state"] == "running" and not c["is_gateway"]
                    and reachable_container(c)}
            for target in self.manual:
                if target["id"] == agent["id"] and target["container"] in rows:
                    key, port = self.hub.lookup(agent["id"], target["container"], target["port"])
                    if port is None:
                        continue
                    destinations[key] = f"docker:{rows[target['container']]['id']}:{target['port']}"
        if not agent["enabled"]:
            destinations = {}
        expires = max(report.get("valid_at", 0), report.get("seen_at", 0) if manual_live else 0) + agent["timeout"]
        self.hub.targets(agent["id"], destinations, ttl=max(0, expires - now))

    def report(self, authorization, raw, manual_routing=False):
        snapshot = inventory(raw)
        with self.lock:
            identity = self.authenticate(authorization)
            now = self.clock()
            if 0 <= now - self.rate.get(identity, -100) < 1:
                raise PermissionError("Agent reports are limited to one per second")
            self.rate[identity] = now
            old = self.reports.get(identity, {})
            state = {**old, "seen_at": now, "inventory": snapshot, "error": "", "manual_routing": manual_routing}
            routes, errors = agent_routes(snapshot)
            if not snapshot["connected"]:
                errors = ["Agent Docker unavailable"]
            elif snapshot["truncated"]:
                errors = ["Agent inventory is incomplete (500 containers)"]
            elif not self.hub.connected(identity):
                errors = ["Agent tunnel is not connected"]
            if not errors:
                try:
                    self.hub.check_capacity([self.hub.key(identity, r["container"], r["spec"]["port"]) for r in routes])
                except ValueError as exc:
                    errors = [str(exc)]
                    # A new target over quota must not expire working targets or
                    # prevent their destination changing after container recreation.
                    state.update(valid_at=now, routes=[r for r in routes if self.hub.lookup(
                        identity, r["container"], r["spec"]["port"])[1] is not None])
            if errors:
                state["error"] = "\n".join(errors)[:8000]
            else:
                # Inventory is data, not authorization to allocate persistent listeners.
                state.update(valid_at=now, routes=routes)
                state.pop("entries", None)
                state.pop("destinations", None)
            write_json(self.directory / (identity + ".json"), state)
            self.reports[identity] = state
            self.refresh_targets(self.find(identity), state)
            return dict(ok=not bool(state["error"]), error=state["error"], received_at=now)

    def snapshot(self, identity=None):
        with self.lock:
            definitions = [self.find(identity)] if identity else self.definitions
            agents = []
            now = self.clock()
            for agent in definitions:
                report = self.reports.get(agent["id"], {})
                age = now - report.get("seen_at", 0)
                connected = self.hub.connected(agent["id"])
                status = ("disabled" if not agent["enabled"] else "waiting" if not report else
                          "offline" if not 0 <= age < agent["timeout"] else "error" if report.get("error") else
                          "online" if connected else "reconnecting")
                value = {k: v for k, v in agent.items() if k != "token_hash"}
                rows = report.get("inventory", {}).get("containers", [])
                value.update(status=status, tunnel=connected, last_seen=report.get("seen_at"),
                             manual_routing=report.get("manual_routing", False),
                             docker_connected=report.get("inventory", {}).get("connected", False),
                             truncated=report.get("inventory", {}).get("truncated", False),
                             error=report.get("error", ""), containers=len(rows), running=sum(c["state"] == "running" for c in rows),
                             routes=len(report.get("routes", report.get("entries", []))), engine_version=report.get("inventory", {}).get("engine_version", ""))
                if identity:
                    value["inventory"] = rows
                agents.append(value)
            return dict(revision=self.revision(), agents=agents)

    def routing(self):
        with self.lock:
            entries, warnings = [], []
            now = self.clock()
            for agent in self.definitions:
                report = self.reports.get(agent["id"], {})
                age = now - report.get("valid_at", 0)
                if agent["enabled"] and 0 <= age < agent["timeout"]:
                    if "routes" not in report:
                        entries += copy.deepcopy(report.get("entries", []))
                    for route in report.get("routes", []):
                        key, port = self.hub.lookup(agent["id"], route["container"], route["spec"]["port"])
                        address = "127.0.0.1" if port is not None else self.pending_address(key)
                        entries.append(dict(spec=copy.deepcopy(route["spec"]), container=route["container"],
                            source="agent_" + agent["id"], target=dict(address=address,
                            port=port or route["spec"]["port"], weight=route["spec"]["weight"], backup=route["spec"]["backup"])))
                self.refresh_targets(agent, report)
                if agent["enabled"] and (report.get("error") or report and age >= agent["timeout"]):
                    warnings.append(agent["name"] + ": " + (report.get("error") or "Agent lease expired"))
            return entries, warnings, self.file.exists()

    @staticmethod
    def pending_address(key):
        # Only appears in review output; materialize replaces it before saving/applying.
        return "pending-" + hashlib.sha256(key.encode()).hexdigest()[:32] + ".invalid"

    @contextmanager
    def materialize(self, *configs, retain=None):
        with self.lock:
            pending = {}
            for agent in self.definitions:
                report = self.reports.get(agent["id"], {})
                if not agent["enabled"] or not 0 <= self.clock() - report.get("valid_at", 0) < agent["timeout"]:
                    continue
                for route in report.get("routes", []):
                    target = (agent["id"], route["container"], route["spec"]["port"])
                    pending[self.pending_address(self.hub.key(*target))] = target
            selected = {}
            def visit(value, convert=False, ports=None):
                if isinstance(value, dict):
                    address = value.get("address", "")
                    if isinstance(address, str) and address.startswith("pending-") and address.endswith(".invalid"):
                        if address not in pending:
                            raise ConflictError("Agent inventory changed. Review routes again.")
                        selected[address] = pending[address]
                        if convert:
                            value["address"] = "127.0.0.1"
                            value["port"] = ports[self.hub.key(*pending[address])]
                    for child in value.values():
                        visit(child, convert, ports)
                elif isinstance(value, list):
                    for child in value:
                        visit(child, convert, ports)
            result = copy.deepcopy(configs)
            visit(list(result))
            with self.hub.reserve(selected.values(), retain=retain) as ports:
                visit(list(result), True, ports)
                for agent in self.definitions:
                    self.refresh_targets(agent, self.reports.get(agent["id"], {}))
                yield result
