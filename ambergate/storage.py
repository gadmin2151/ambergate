"""Local immutable revisions and verified nginx reloads."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from .config import default_config, validate
from .nginx import digest, render
from .metrics import Metrics


class ConflictError(ValueError):
    pass


class ApplyError(RuntimeError):
    pass


def atomic_write(path, data):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


class Store:
    def __init__(self, data_dir, cache_dir, run_dir, nginx_bin="nginx", port=80,
                 control_port=8084, mime_types="/etc/nginx/mime.types", tls_port=443):
        self.data = Path(data_dir).resolve()
        self.revisions = self.data / "revisions"
        self.active = self.data / "active"
        self.draft = self.data / "draft.json"
        self.lock = threading.RLock()
        self.nginx_bin = nginx_bin
        self.options = dict(cache_dir=str(Path(cache_dir).resolve()),
                            run_dir=str(Path(run_dir).resolve()), port=port,
                            control_port=control_port, mime_types=mime_types,
                            tls_dir=str(self.data / "tls"), tls_port=tls_port)
        self.process = None
        self.started = time.time()
        self.last_error = None
        self.metrics = Metrics(self.options["run_dir"])
        for path in (self.data, self.revisions, Path(cache_dir), Path(run_dir)):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data, 0o700)
        if not self.active.exists():
            generation = self.prepare(default_config())
            self.check(generation)
            self.activate(generation)
        if not self.draft.exists():
            write_json(self.draft, self.active_config())

    def active_id(self):
        return self.active.resolve().name

    def active_config(self):
        return validate(json.loads((self.active / "config.json").read_text()))

    def draft_config(self):
        return validate(json.loads(self.draft.read_text()))

    def referenced_agent_ports(self):
        """Include partially committed generations when deciding allocation rollback."""
        with self.lock:
            ports = set()
            def visit(value):
                if isinstance(value, dict):
                    if value.get("address") == "127.0.0.1" and type(value.get("port")) is int:
                        ports.add(value["port"])
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            for path in [self.draft, self.active / "config.json", *self.revisions.glob("*/config.json")]:
                visit(json.loads(path.read_text()))
            return ports

    def prepare(self, config):
        config = validate(config)
        generation = uuid.uuid4().hex
        directory = self.revisions / generation
        directory.mkdir()
        write_json(directory / "config.json", config)
        atomic_write(directory / "nginx.conf", render(config, generation, **self.options))
        write_json(directory / "meta.json", {"id": generation, "created_at": time.time(), "applied_at": None})
        sync_directory(self.revisions)
        return generation

    def check(self, generation):
        command = [self.nginx_bin, "-t", "-c", str(self.revisions / generation / "nginx.conf")]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=12)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApplyError(f"Не удалось проверить Nginx: {exc}") from exc
        if result.returncode:
            raise ApplyError(result.stderr[-8000:])
        return result.stderr[-8000:]

    def activate(self, generation):
        temp = self.data / ("active." + uuid.uuid4().hex)
        try:
            temp.symlink_to(Path("revisions") / generation, target_is_directory=True)
            os.replace(temp, self.active)
            sync_directory(self.data)
        finally:
            temp.unlink(missing_ok=True)

    def running_generation(self):
        # Do not route loopback probes through environment HTTP proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{self.options['control_port']}/generation", timeout=.5) as response:
                return response.read(128).decode()
        except (OSError, urllib.error.URLError):
            return None

    def wait_generation(self, generation, timeout=8):
        deadline = time.monotonic() + timeout
        confirmed_since = None
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                return False
            if self.running_generation() == generation:
                # Nginx starts new workers, waits 100 ms, then retires the old
                # listeners. One successful probe can land in that overlap.
                # Require a stable generation across the handoff; existing
                # long requests and WebSockets may still drain normally.
                if confirmed_since is None:
                    confirmed_since = time.monotonic()
                elif time.monotonic() - confirmed_since >= .2:
                    return True
            else:
                confirmed_since = None
            time.sleep(.08)
        return False

    def start(self):
        with self.lock:
            # Refresh generated runtime directives after upgrades, using only the
            # ACTIVE settings. A saved draft must never be applied on restart.
            active = self.active_config()
            if (self.active / "nginx.conf").read_text() != render(active, self.active_id(), **self.options):
                generation = self.prepare(active)
                try:
                    self.check(generation)
                except Exception:
                    shutil.rmtree(self.revisions / generation)
                    raise
                self.activate(generation)
            self.check(self.active_id())
            self.metrics.start()
            try:
                self.process = subprocess.Popen([self.nginx_bin, "-c", str(self.active / "nginx.conf"), "-g", "daemon off;"])
                if not self.wait_generation(self.active_id()):
                    raise ApplyError("Nginx не запустился: проверьте логи и занятые порты")
            except Exception:
                self.stop()
                raise
            self.started = time.time()
            self.mark_applied(self.active_id())
            self.cleanup()

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGQUIT)
            try:
                self.process.wait(timeout=31)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.metrics.stop()

    def mark_applied(self, generation):
        path = self.revisions / generation / "meta.json"
        meta = json.loads(path.read_text())
        if not meta["applied_at"]:
            meta["applied_at"] = time.time()
            write_json(path, meta)

    def expect_revision(self, revision):
        config = self.draft_config()
        if revision != digest(config):
            raise ConflictError("Черновик изменился. Обновите страницу перед сохранением.")
        return config

    def save(self, config, revision):
        with self.lock:
            self.expect_revision(revision)
            write_json(self.draft, validate(config))
            return self.snapshot()

    def preview(self, config):
        return render(validate(config), "preview", **self.options)

    def test(self, revision):
        with self.lock:
            generation = self.prepare(self.expect_revision(revision))
            try:
                return self.check(generation)
            finally:
                shutil.rmtree(self.revisions / generation)

    def apply(self, revision):
        with self.lock:
            return self.apply_config(self.expect_revision(revision))

    def apply_config(self, config):
        """Activate a candidate without promoting unrelated draft changes."""
        with self.lock:
            old = self.active_id()
            generation = self.prepare(config)
            try:
                self.check(generation)
                if not self.process or self.process.poll() is not None:
                    raise ApplyError("Nginx не работает; перезапустите контейнер")
                self.activate(generation)
                self.process.send_signal(signal.SIGHUP)
                if not self.wait_generation(generation):
                    raise ApplyError("Nginx не подтвердил новую конфигурацию")
            except Exception as exc:
                if self.active_id() == generation:
                    self.activate(old)
                    if self.process and self.process.poll() is None:
                        self.process.send_signal(signal.SIGHUP)
                        if not self.wait_generation(old):
                            exc = ApplyError(f"{exc}. Откат не подтверждён; проверьте контейнер.")
                self.last_error = str(exc)
                shutil.rmtree(self.revisions / generation)
                raise ApplyError(str(exc)) from exc
            self.mark_applied(generation)
            self.last_error = None
            self.cleanup()
            return self.snapshot()

    def restore(self, generation, revision):
        with self.lock:
            self.expect_revision(revision)
            if not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation):
                raise ValueError("Неверная версия")
            path = self.revisions / generation / "config.json"
            if not path.is_file():
                raise ValueError("Версия не найдена")
            write_json(self.draft, validate(json.loads(path.read_text())))
            return self.snapshot()

    def history(self):
        result = []
        for directory in self.revisions.iterdir():
            if (directory / "meta.json").is_file():
                meta = json.loads((directory / "meta.json").read_text())
                if meta["applied_at"] or directory.name == self.active_id():
                    config = json.loads((directory / "config.json").read_text())
                    result.append({**meta, "active": directory.name == self.active_id(), "hosts": len(config["hosts"])})
        return sorted(result, key=lambda item: item["created_at"], reverse=True)

    def cleanup(self):
        for meta in self.history()[20:]:
            if not meta["active"]:
                shutil.rmtree(self.revisions / meta["id"])

    def snapshot(self):
        with self.lock:
            config = self.draft_config()
            return {"config": config, "revision": digest(config),
                    "active_revision": digest(self.active_config()), "generation": self.active_id(),
                    "pending": config != self.active_config(), "history": self.history()}

    def status(self):
        running = self.process is not None and self.process.poll() is None
        return {"running": running, "healthy": running and self.running_generation() == self.active_id(),
                "uptime": int(time.time() - self.started), "last_error": self.last_error}

    def dashboard(self):
        with self.lock:
            active = self.active_config()
            status = self.status()
            meta = json.loads((self.active / "meta.json").read_text())
            configuration = dict(generation=self.active_id(), applied_at=meta["applied_at"],
                                 revision=digest(self.draft_config()),
                                 pending=self.draft_config() != active, hosts=active["hosts"],
                                 settings=active["settings"])
        connections = None
        if status["running"]:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(f"http://127.0.0.1:{self.options['control_port']}/status", timeout=.5) as response:
                    text = response.read(1024).decode()
                counts = re.search(r"Reading: (\d+) Writing: (\d+) Waiting: (\d+)", text)
                if counts:
                    reading, writing, waiting = map(int, counts.groups())
                    # stub_status includes its own connection in Writing.
                    writing = max(0, writing - 1)
                    connections = dict(reading=reading, writing=writing, waiting=waiting,
                                       active=reading + writing + waiting)
            except (OSError, urllib.error.URLError, ValueError):
                pass
        return {**self.metrics.snapshot(), "nginx": status, "connections": connections,
                "configuration": configuration}
