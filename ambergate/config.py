"""Strict structured configuration. User input is never an nginx directive."""
import copy
import ipaddress
import re


class ValidationError(ValueError):
    pass


DEFAULT = {
    "version": 1,
    "settings": {
        "resolvers": ["127.0.0.11"],
        "trusted_proxies": [],
        "rate_rps": 20,
        "rate_burst": 40,
        "connections_per_ip": 50,
        "body_mb": 100,
        "cache_mb": 1024,
        "block_dotfiles": True,
        "security_headers": True,
    },
    "hosts": [],
}


def default_config():
    return copy.deepcopy(DEFAULT)


def fail(path, message):
    raise ValidationError(f"{path}: {message}")


def obj(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        fail(path, "неверный набор полей")


def integer(value, low, high, path):
    if type(value) is not int or not low <= value <= high:
        fail(path, f"ожидается целое число {low}–{high}")


def boolean(value, path):
    if type(value) is not bool:
        fail(path, "ожидается true/false")


def sequence(value, low, high, path):
    if not isinstance(value, list) or not low <= len(value) <= high:
        fail(path, f"ожидается список, элементов {low}–{high}")


def hostname(value, path):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(
        r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value
    ):
        fail(path, "ожидается DNS-имя или IPv4, без схемы и пути")
    if any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", x)
           for x in value.split(".")):
        fail(path, "некорректное DNS-имя")


def upstream_hostname(value, path):
    # Docker container names may contain underscores. Domain names remain strict.
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(
        r"[a-zA-Z0-9_](?:[a-zA-Z0-9_.-]*[a-zA-Z0-9_])?", value
    ) or any(not re.fullmatch(r"[a-zA-Z0-9_](?:[a-zA-Z0-9_-]{0,61}[a-zA-Z0-9_])?", x)
             for x in value.split(".")):
        fail(path, "ожидается имя сервера или Docker-контейнера, без схемы и пути")


def route_targets(route):
    return route["targets"] + route.get("docker", {}).get("targets", [])


def validate(config):
    obj(config, DEFAULT, "config")
    if type(config["version"]) is not int or config["version"] != 1:
        fail("version", "поддерживается версия 1")
    s = config["settings"]
    obj(s, DEFAULT["settings"], "settings")
    for key, low, high in [("rate_rps", 0, 100000), ("rate_burst", 0, 100000),
                           ("connections_per_ip", 0, 65535), ("body_mb", 1, 102400),
                           ("cache_mb", 1, 1048576)]:
        integer(s[key], low, high, key)
    for key in ("block_dotfiles", "security_headers"):
        boolean(s[key], key)
    for key, low in (("resolvers", 1), ("trusted_proxies", 0)):
        sequence(s[key], low, 32, key)
        for value in s[key]:
            try:
                if not isinstance(value, str) or "%" in value:
                    raise ValueError()
                if key == "resolvers":
                    ipaddress.ip_address(value)
                else:
                    ipaddress.ip_network(value, strict=False)
            except ValueError:
                fail(key, "ожидается IP" if key == "resolvers" else "ожидается IP или CIDR")
    sequence(config["hosts"], 0, 100, "hosts")
    domains, ids = set(), set()
    for hi, host in enumerate(config["hosts"]):
        hp = f"hosts[{hi}]"
        obj(host, ("id", "domain", "enabled", "routes"), hp)
        check_id(host["id"], ids, hp)
        hostname(host["domain"], hp + ".domain")
        if host["domain"].lower() in domains:
            fail(hp, "домен уже существует")
        domains.add(host["domain"].lower())
        boolean(host["enabled"], hp + ".enabled")
        sequence(host["routes"], 0, 50, hp + ".routes")
        paths = set()
        for ri, route in enumerate(host["routes"]):
            rp = f"{hp}.routes[{ri}]"
            if not isinstance(route, dict):
                fail(rp, "неверный набор полей")
            obj({k: v for k, v in route.items() if k != "docker"}, ("id", "name", "path", "strip_prefix", "balance", "targets",
                        "cache", "cache_ttl", "websocket", "timeout", "body_mb",
                        "rate_rps", "rate_burst"), rp)
            check_id(route["id"], ids, rp)
            if not isinstance(route["name"], str) or not 1 <= len(route["name"]) <= 80:
                fail(rp, "название: 1–80 символов")
            path = route["path"]
            if not isinstance(path, str) or len(path) > 200 or not re.fullmatch(
                r"/(?:[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*)?", path
            ):
                fail(rp + ".path", "путь вида /, /api, /v1/api; без завершающего /")
            if path in paths:
                fail(rp, "путь уже существует")
            paths.add(path)
            for key in ("strip_prefix", "cache", "websocket"):
                boolean(route[key], rp + "." + key)
            if route["balance"] not in ("round_robin", "least_conn", "ip_hash"):
                fail(rp, "неизвестный алгоритм балансировки")
            for key, low, high in (("cache_ttl", 1, 604800), ("timeout", 1, 3600),
                                   ("body_mb", 0, 102400), ("rate_rps", 0, 100000),
                                   ("rate_burst", 0, 100000)):
                integer(route[key], low, high, rp + "." + key)
            if "docker" in route:
                docker = route["docker"]
                if not isinstance(docker, dict):
                    fail(rp, "неверный набор полей")
                obj({k: v for k, v in docker.items() if k != "sources"}, ("managed", "group", "targets"), rp + ".docker")
                boolean(docker["managed"], rp + ".docker.managed")
                if not isinstance(docker["group"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{0,64}", docker["group"]):
                    fail(rp, "Docker group: 1–64 символа, буквы, цифры, _ или -")
                if not docker["managed"] and not docker["group"]:
                    fail(rp, "Укажите Docker group")
                if docker["managed"] and (docker["group"] or route["targets"]):
                    fail(rp, "Label-маршрут использует только обнаруженные серверы")
                sequence(docker["targets"], 0, 32, rp + ".docker.targets")
                if "sources" in docker:
                    sources = docker["sources"]
                    if not isinstance(sources, dict) or len(sources) > 33:
                        fail(rp, "Invalid Docker sources")
                    recorded = []
                    for source, members in sources.items():
                        if not re.fullmatch(r"local|agent_[a-f0-9]{32}", source):
                            fail(rp, "Invalid Docker source ID")
                        sequence(members, 0, 32, rp + ".docker.sources")
                        for member in members:
                            if member not in docker["targets"]:
                                fail(rp, "Docker source target does not match route")
                            if member not in recorded:
                                recorded.append(member)
                    if any(t not in recorded for t in docker["targets"]):
                        fail(rp, "Docker target has no source")
            sequence(route["targets"], 0 if "docker" in route else 1, 32, rp + ".targets")
            targets = route_targets(route)
            sequence(targets, 0 if "docker" in route else 1, 32, rp + ".targets")
            for target in targets:
                if not isinstance(target, dict):
                    fail(rp, "неверный набор полей")
                obj({k: v for k, v in target.items() if k != "agent"}, ("address", "port", "weight", "backup"), rp + ".target")
                if "agent" in target:
                    agent = target["agent"]
                    obj(agent, ("id", "container", "port"), rp + ".target.agent")
                    if not isinstance(agent["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", agent["id"]) or target["address"] != "127.0.0.1":
                        fail(rp, "Invalid agent tunnel target")
                    upstream_hostname(agent["container"], rp + ".target.agent.container")
                    integer(agent["port"], 1, 65535, rp + ".target.agent.port")
                address = target["address"]
                try:
                    if not isinstance(address, str):
                        raise ValueError()
                    ipaddress.ip_address(address)
                    if "%" in address:
                        fail(rp, "IPv6 zone ID не поддерживается")
                except ValueError:
                    upstream_hostname(address, rp + ".address")
                integer(target["port"], 1, 65535, rp + ".port")
                integer(target["weight"], 1, 1000, rp + ".weight")
                boolean(target["backup"], rp + ".backup")
                if target["backup"] and route["balance"] == "ip_hash":
                    fail(rp, "backup несовместим с IP hash")
            if targets and all(t["backup"] for t in targets) and "docker" not in route:
                fail(rp, "нужен хотя бы один основной сервер")
    return copy.deepcopy(config)


def check_id(value, ids, path):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value):
        fail(path, "неверный ID")
    if value in ids:
        fail(path, "ID должен быть уникальным")
    ids.add(value)
