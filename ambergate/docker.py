"""Optional Docker discovery. Only fixed GET operations over a local Unix socket.

No CLI, remote endpoints, container mutations, environment values, mounts or
arbitrary Docker API proxying are exposed to the browser.
"""
from .environment import setting

import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time

from .config import upstream_hostname, ValidationError
from .nginx import digest
from .storage import ConflictError, write_json


class DockerError(Exception):
    pass


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=2)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def settings(value):
    required = {"enabled", "socket_path", "gateway_container"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"host_address"}:
        raise ValueError("Неверный набор настроек Docker")
    if type(value["enabled"]) is not bool:
        raise ValueError("enabled должен быть true/false")
    path = value["socket_path"]
    if (not isinstance(path, str) or not path.startswith("/") or not path.endswith(".sock")
            or len(path.encode()) > 103 or any(ord(c) < 32 for c in path)
            or ".." in Path(path).parts):
        raise ValueError("Укажите абсолютный путь к Unix socket .sock (до 103 байт)")
    name = value["gateway_container"]
    if not isinstance(name, str) or name and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
        raise ValueError("Контейнер AmberGate: имя или ID, без схемы и пути")
    host = value.get("host_address", "")
    if not isinstance(host, str):
        raise ValueError("IP Docker-хоста должен быть строкой")
    if host:
        try:
            address = ipaddress.ip_address(host)
            if "%" in host or address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
                raise ValueError()
        except ValueError:
            raise ValueError("Укажите доступный IPv4 или IPv6 Docker-хоста, без схемы и порта") from None
        host = str(address)
    return {**value, "host_address": host}


def published_targets(ports, host_address="", native=False):
    endpoints, reason = {}, "Нет опубликованных TCP-портов"
    for port in ports:
        public = port.get("PublicPort")
        if type(public) is not int or not 1 <= public <= 65535:
            continue
        try:
            bind = ipaddress.ip_address(port.get("IP") or "0.0.0.0")
        except ValueError:
            continue
        if bind.is_multicast or bind.is_link_local or "%" in str(bind):
            continue
        if bind.is_unspecified:
            if native:
                target = "127.0.0.1" if bind.version == 4 else "::1"
            elif not host_address:
                reason = "Укажите IP Docker-хоста для опубликованных портов"
                continue
            elif ipaddress.ip_address(host_address).version != bind.version:
                reason = "Нет публикации для выбранной версии IP Docker-хоста"
                continue
            else:
                target = host_address
        elif bind.is_loopback and not native:
            reason = "Порт опубликован только на localhost Docker-хоста"
            continue
        else:
            # A publication bound to a specific interface must keep that address.
            target = str(bind)
        endpoints[target, public] = dict(address=target, port=public,
            container_port=port["PrivatePort"], kind="published")
    return sorted(endpoints.values(), key=lambda e: (":" in e["address"], e["port"], e["address"])), reason


class Docker:
    def __init__(self, data_dir, in_container=None):
        self.file = Path(data_dir) / "docker.json"
        self.in_container = Path("/.dockerenv").exists() if in_container is None else in_container
        self.lock = threading.RLock()
        self.cached = None
        self.cached_at = 0
        # An invalid optional environment hint must not stop the HTTP gateway.
        # Settings are validated when the user saves/enables discovery.
        self.defaults = dict(enabled=False,
                                     socket_path=setting("DOCKER_SOCKET", "/var/run/docker.sock"),
                                     gateway_container=setting("DOCKER_CONTAINER", ""),
                                     host_address="")

    def read(self):
        return settings(json.loads(self.file.read_text())) if self.file.exists() else dict(self.defaults)

    def save(self, value, revision):
        with self.lock:
            if revision != digest(self.read()):
                raise ConflictError("Настройки Docker изменились. Обновите страницу.")
            write_json(self.file, settings(value))
            self.cached = None
            return self.snapshot()

    def get(self, path, endpoint):
        connection = UnixConnection(path)
        try:
            connection.request("GET", endpoint, headers={"Accept": "application/json"})
            response = connection.getresponse()
            payload = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200:
                raise DockerError(f"Docker API вернул HTTP {response.status}")
            if len(payload) > 4 * 1024 * 1024:
                raise DockerError("Ответ Docker превышает 4 MiB. Уменьшите число контейнеров.")
            return json.loads(payload)
        except (ValueError, http.client.HTTPException) as exc:
            raise DockerError("Docker вернул некорректный ответ") from exc
        finally:
            connection.close()

    def snapshot(self, force=False):
        with self.lock:
            value = self.read()
            if self.cached and not force and time.monotonic() - self.cached_at < 3:
                return self.cached
            result = dict(settings=value, revision=digest(value), connected=False,
                          containers=[], networks=[], gateway_networks=[], checked_at=time.time(),
                          mode="container" if self.in_container or value["gateway_container"] else "host",
                          engine_version=None, message="Подключение к Docker выключено", truncated=False)
            if value["enabled"]:
                try:
                    self.discover(value, result)
                except FileNotFoundError:
                    result["message"] = "Socket не найден. Смонтируйте docker.sock и проверьте путь."
                except PermissionError:
                    result["message"] = "Нет доступа к socket. Проверьте права процесса AmberGate."
                except (socket.timeout, TimeoutError):
                    result["message"] = "Docker не ответил за отведённое время."
                except DockerError as exc:
                    result["message"] = str(exc)
                except OSError:
                    result["message"] = "Не удалось подключиться к Docker. Проверьте daemon и socket."
                except (KeyError, TypeError, ValueError, AttributeError):
                    result["message"] = "Неожиданный формат ответа Docker API."
            self.cached, self.cached_at = result, time.monotonic()
            return result

    def discover(self, value, result):
        path = value["socket_path"]
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            raise DockerError("Указанный путь не является Unix socket")
        version = self.get(path, "/version")
        api = version.get("ApiVersion", "")
        if not isinstance(api, str) or not re.fullmatch(r"1\.\d{1,3}", api):
            raise DockerError("Неподдерживаемая версия Docker Engine API")
        prefix = "/v" + api
        containers = self.get(path, prefix + "/containers/json?all=1&limit=500")
        if not isinstance(containers, list):
            raise DockerError("Docker не вернул список контейнеров")
        shared, reachable, self_id, network_message = {}, {}, "", ""
        if result["mode"] == "container":
            identifier = value["gateway_container"] or socket.gethostname()
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", identifier):
                raise DockerError("Укажите имя или ID контейнера AmberGate в настройках")
            try:
                own = self.get(path, prefix + "/containers/" + identifier + "/json")
                shared = own.get("NetworkSettings", {}).get("Networks", {})
                self_id = own["Id"]
                if own.get("HostConfig", {}).get("NetworkMode") == "host":
                    result["mode"] = "host-network"
            except DockerError:
                network_message = "Не удалось определить сети AmberGate. Укажите его имя или ID в настройках."
        if result["mode"] == "host-network":
            # Host networking on native Linux can reach local bridge IPs, but
            # does not provide Docker's embedded DNS or access to every driver.
            networks = self.get(path, prefix + "/networks")
            if not isinstance(networks, list):
                raise DockerError("Docker не вернул список сетей")
            reachable = {n["Name"]: {"NetworkID": n["Id"]} for n in networks
                         if n.get("Driver") == "bridge" and n.get("Scope") == "local"
                         and n.get("Name") and n.get("Id")}
        rows = [self.container(c, shared, self_id, result["mode"], value["host_address"], reachable)
                for c in containers[:500]]
        result.update(connected=True, engine_version=str(version.get("Version", ""))[:80],
                      gateway_networks=sorted(shared), truncated=len(containers) >= 500,
                      message=network_message or ("Host network: доступ к внутренним IP локальных bridge-сетей" if result["mode"] == "host-network" else
                              "Общая сеть Docker или опубликованные порты хоста" if shared else
                              "Локальный запуск: используйте опубликованные TCP-порты" if result["mode"] == "host" else
                              "У AmberGate нет подключённых сетей Docker"))
        result["containers"] = sorted(rows, key=lambda c: (not c["selectable"], c["name"]))
        result["networks"] = sorted({n for c in rows for n in c["networks"]})

    def container(self, c, shared, self_id, mode, host_address="", reachable=None):
        name = str((c.get("Names") or [str(c.get("Id", ""))[:12]])[0]).lstrip("/")[:253]
        labels = c.get("Labels") or {}
        nets = c.get("NetworkSettings", {}).get("Networks", {})
        ports = [p for p in c.get("Ports", []) if p.get("Type") == "tcp"
                 and type(p.get("PrivatePort")) is int and 1 <= p["PrivatePort"] <= 65535]
        common = [n for n, v in nets.items() if n in shared and v.get("NetworkID")
                  and v.get("NetworkID") == shared[n].get("NetworkID")]
        available = [n for n, v in nets.items() if n in (reachable or {}) and v.get("NetworkID")
                     and v.get("NetworkID") == reachable[n].get("NetworkID")] if mode == "host-network" else common
        row = dict(id=str(c.get("Id", ""))[:64], name=name, image=str(c.get("Image", ""))[:300],
                   state=str(c.get("State", "unknown"))[:30], status=str(c.get("Status", ""))[:150],
                   project=str(labels.get("com.docker.compose.project", ""))[:100],
                   service=str(labels.get("com.docker.compose.service", ""))[:100],
                   networks=sorted(nets), shared_networks=sorted(common), reachable_networks=sorted(available), endpoints=[],
                   ports=sorted({p["PrivatePort"] for p in ports}), selectable=False, reason="",
                   host_endpoints=[], host_reason="", is_gateway=c.get("Id") == self_id,
                   route_labels={k: str(v)[:4097] for k, v in sorted(labels.items())
                                 if k == "ambergate.route" or k.startswith("ambergate.route.")})
        published, host_reason = published_targets(ports, host_address, native=mode in ("host", "host-network"))
        if c.get("Id") == self_id:
            row["reason"] = "Это сам AmberGate"
        elif c.get("State") != "running":
            row["reason"] = "Контейнер не запущен"
        elif mode == "host":
            row["endpoints"] = published
            row["reason"] = host_reason if not published else ""
        elif not available:
            row["reason"] = "Нет доступной локальной bridge-сети" if mode == "host-network" else "Нет общей сети с AmberGate"
        else:
            dns = mode != "host-network" and any(n not in ("bridge", "host", "none") and nets[n].get("IPAddress") for n in available)
            try:
                upstream_hostname(name, "Docker container")
            except ValidationError:
                dns = False
            if dns:
                row["endpoints"].append(dict(address=name, kind="dns"))
            else:
                for n in available:
                    for key in ("IPAddress", "GlobalIPv6Address"):
                        try:
                            ip = ipaddress.ip_address(nets[n].get(key, ""))
                            if ip.is_unspecified or ip.is_loopback or ip.is_multicast or ip.is_link_local:
                                continue
                            address = str(ip)
                        except ValueError:
                            continue
                        row["endpoints"].append(dict(address=address, kind="ip", network=n))
                row["reason"] = "Нет адреса в общей сети" if not row["endpoints"] else ""
        if c.get("Id") != self_id and c.get("State") == "running":
            row["host_endpoints"] = published
            row["host_reason"] = "" if published else host_reason
            if not row["endpoints"] and published:
                row["endpoints"], row["reason"] = published, ""
        else:
            row["host_reason"] = row["reason"]
        row["selectable"] = bool(row["endpoints"])
        # Docker can publish the same target more than once (e.g. v4 and v6).
        unique = {(e["address"], e.get("port")): e for e in row["endpoints"]}
        row["endpoints"] = sorted(unique.values(), key=lambda e: (":" in e["address"], e.get("port", 0), e["address"]))
        return row
