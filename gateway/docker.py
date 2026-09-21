"""Optional Docker discovery. Only fixed GET operations over a local Unix socket.

No CLI, remote endpoints, container mutations, environment values, mounts or
arbitrary Docker API proxying are exposed to the browser.
"""
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
    if not isinstance(value, dict) or set(value) != {"enabled", "socket_path", "gateway_container"}:
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
        raise ValueError("Контейнер gateway: имя или ID, без схемы и пути")
    return dict(value)


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
                                     socket_path=os.environ.get("GATEWAY_DOCKER_SOCKET", "/var/run/docker.sock"),
                                     gateway_container=os.environ.get("GATEWAY_DOCKER_CONTAINER", ""))

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
                    result["message"] = "Нет доступа к socket. Проверьте права процесса Gateway."
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
        shared, self_id, network_message = {}, "", ""
        if result["mode"] == "container":
            identifier = value["gateway_container"] or socket.gethostname()
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", identifier):
                raise DockerError("Укажите имя или ID контейнера gateway в настройках")
            try:
                own = self.get(path, prefix + "/containers/" + identifier + "/json")
                shared = own.get("NetworkSettings", {}).get("Networks", {})
                self_id = own["Id"]
            except DockerError:
                network_message = "Не удалось определить сети gateway. Укажите его имя или ID в настройках."
        rows = [self.container(c, shared, self_id, result["mode"]) for c in containers[:500]]
        result.update(connected=True, engine_version=str(version.get("Version", ""))[:80],
                      gateway_networks=sorted(shared), truncated=len(containers) >= 500,
                      message=network_message or ("Контейнеры доступны через общие сети Docker" if shared else
                              "Локальный запуск: используйте опубликованные TCP-порты" if result["mode"] == "host" else
                              "У gateway нет подключённых сетей Docker"))
        result["containers"] = sorted(rows, key=lambda c: (not c["selectable"], c["name"]))
        result["networks"] = sorted({n for c in rows for n in c["networks"]})

    def container(self, c, shared, self_id, mode):
        name = str((c.get("Names") or [str(c.get("Id", ""))[:12]])[0]).lstrip("/")[:253]
        labels = c.get("Labels") or {}
        nets = c.get("NetworkSettings", {}).get("Networks", {})
        ports = [p for p in c.get("Ports", []) if p.get("Type") == "tcp"
                 and type(p.get("PrivatePort")) is int and 1 <= p["PrivatePort"] <= 65535]
        common = [n for n, v in nets.items() if n in shared and v.get("NetworkID")
                  and v.get("NetworkID") == shared[n].get("NetworkID")]
        row = dict(id=str(c.get("Id", ""))[:64], name=name, image=str(c.get("Image", ""))[:300],
                   state=str(c.get("State", "unknown"))[:30], status=str(c.get("Status", ""))[:150],
                   project=str(labels.get("com.docker.compose.project", ""))[:100],
                   service=str(labels.get("com.docker.compose.service", ""))[:100],
                   networks=sorted(nets), shared_networks=sorted(common), endpoints=[],
                   ports=sorted({p["PrivatePort"] for p in ports}), selectable=False, reason="")
        if c.get("Id") == self_id:
            row["reason"] = "Это сам gateway"
        elif c.get("State") != "running":
            row["reason"] = "Контейнер не запущен"
        elif mode == "host":
            for p in ports:
                public = p.get("PublicPort")
                if type(public) is not int or not 1 <= public <= 65535:
                    continue
                try:
                    address = ipaddress.ip_address(p.get("IP") or "0.0.0.0")
                except ValueError:
                    continue
                target = "127.0.0.1" if address == ipaddress.ip_address("0.0.0.0") else "::1" if address.is_unspecified else str(address)
                row["endpoints"].append(dict(address=target, port=public, container_port=p["PrivatePort"], kind="published"))
            row["reason"] = "Нет опубликованных TCP-портов" if not row["endpoints"] else ""
        elif not common:
            row["reason"] = "Нет общей сети с gateway"
        else:
            dns = any(n not in ("bridge", "host", "none") and nets[n].get("IPAddress") for n in common)
            try:
                upstream_hostname(name, "Docker container")
            except ValidationError:
                dns = False
            if dns:
                row["endpoints"].append(dict(address=name, kind="dns"))
            else:
                for n in common:
                    for key in ("IPAddress", "GlobalIPv6Address"):
                        try:
                            address = str(ipaddress.ip_address(nets[n].get(key, "")))
                        except ValueError:
                            continue
                        row["endpoints"].append(dict(address=address, kind="ip", network=n))
                row["reason"] = "Нет адреса в общей сети" if not row["endpoints"] else ""
        row["selectable"] = bool(row["endpoints"])
        # Docker can publish the same target more than once (e.g. v4 and v6).
        unique = {(e["address"], e.get("port")): e for e in row["endpoints"]}
        row["endpoints"] = sorted(unique.values(), key=lambda e: (":" in e["address"], e.get("port", 0), e["address"]))
        return row
