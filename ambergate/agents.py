"""Agent identities, bounded inventory and source-scoped routing leases."""
import copy
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


def wire_snapshot(snapshot):
    """Allow-list metadata; never send Docker env, mounts or unrelated labels."""
    rows = []
    for row in snapshot["containers"]:
        clean = {key: row[key] for key in ("id", "name", "image", "state", "status", "project", "service",
                 "networks", "shared_networks", "ports", "route_labels", "is_gateway")}
        clean["endpoints"] = [{"address": e["address"], "kind": e["kind"]} for e in row["endpoints"]
                              if e["kind"] in ("dns", "ip")]
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
                    raise ValueError("Connect the agent and application to a shared Docker network")
                entries.append(dict(spec=spec, container=row["name"], address=row["endpoints"][0]["address"],
                                    target_id=target_id(row["id"], spec["port"])))
            except ValueError as exc:
                errors.append(f"{row['name']} / {key}: {exc}")
    return entries, errors[:100]


class Agents:
    def __init__(self, data, hub, clock=time.time):
        self.data, self.hub, self.clock = data, hub, clock
        self.file = data / "agents.json"
        self.directory = data / "agents"
        self.directory.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.definitions = json.loads(self.file.read_text()) if self.file.exists() else []
        self.reports, self.rate = {}, {}
        for agent in self.definitions:
            path = self.directory / (agent["id"] + ".json")
            if path.exists():
                self.reports[agent["id"]] = json.loads(path.read_text())

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

    def report(self, authorization, raw):
        snapshot = inventory(raw)
        with self.lock:
            identity = self.authenticate(authorization)
            now = self.clock()
            if 0 <= now - self.rate.get(identity, -100) < 1:
                raise PermissionError("Agent reports are limited to one per second")
            self.rate[identity] = now
            old = self.reports.get(identity, {})
            state = {**old, "seen_at": now, "inventory": snapshot, "error": ""}
            routes, errors = agent_routes(snapshot)
            if not snapshot["connected"]:
                errors = ["Agent Docker unavailable"]
            elif snapshot["truncated"]:
                errors = ["Agent inventory is incomplete (500 containers)"]
            elif not self.hub.connected(identity):
                errors = ["Agent tunnel is not connected"]
            if errors:
                state["error"] = "\n".join(errors)[:8000]
            else:
                destinations, entries = {}, []
                for route in routes:
                    key, port = self.hub.endpoint(identity, route["container"], route["spec"]["port"])
                    destinations[key] = route["target_id"]
                    entries.append(dict(spec=route["spec"], container=route["container"], source="agent_" + identity,
                                        target=dict(address="127.0.0.1", port=port, weight=route["spec"]["weight"], backup=route["spec"]["backup"])))
                state.update(valid_at=now, entries=entries, destinations=destinations)
            write_json(self.directory / (identity + ".json"), state)
            self.reports[identity] = state
            if not state["error"]:
                self.hub.targets(identity, state["destinations"])
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
                             error=report.get("error", ""), containers=len(rows), running=sum(c["state"] == "running" for c in rows),
                             routes=len(report.get("entries", [])), engine_version=report.get("inventory", {}).get("engine_version", ""))
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
                    entries += copy.deepcopy(report.get("entries", []))
                else:
                    self.hub.targets(agent["id"], {})
                if agent["enabled"] and (report.get("error") or report and age >= agent["timeout"]):
                    warnings.append(agent["name"] + ": " + (report.get("error") or "Agent lease expired"))
            return entries, warnings, self.file.exists()
