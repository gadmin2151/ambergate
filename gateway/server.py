"""Small private control plane; only nginx handles gateway traffic."""
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .storage import ApplyError, ConflictError
from .docker import Docker

STATIC = Path(__file__).parent / "static"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, store, auth):
        self.store = store
        self.auth = auth
        self.docker = Docker(store.data)
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "Gateway"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # Request bodies/passwords and query strings never enter admin logs.
        print(f"admin {self.client_address[0]} {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}", flush=True)

    def respond(self, status, value, mime="application/json; charset=utf-8", headers=None):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False).encode()
        elif isinstance(value, str):
            value = value.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(value)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, val in (headers or {}).items():
            self.send_header(name, val)
        self.end_headers()
        self.wfile.write(value)

    def token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie["gateway_session"].value if "gateway_session" in cookie else ""
        except CookieError:
            return ""

    def cookie(self, token, age=43200):
        secure = "; Secure" if os.environ.get("GATEWAY_SECURE_COOKIE", "false").lower() == "true" else ""
        return f"gateway_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}{secure}"

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/healthz":
                healthy = self.server.store.status()["healthy"]
                return self.respond(200 if healthy else 503, {"healthy": healthy})
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                     "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                     "/docker.js": ("docker.js", "text/javascript; charset=utf-8"),
                     "/docker.css": ("docker.css", "text/css; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8"),
                     "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path in files:
                name, mime = files[path]
                return self.respond(200, (STATIC / name).read_bytes(), mime)
            session = self.server.auth.session(self.token())
            if not session:
                return self.respond(401, {"error": "Войдите в панель управления"})
            if path == "/api/session":
                return self.respond(200, {"csrf": session["csrf"]})
            if path == "/api/config":
                return self.respond(200, self.server.store.snapshot())
            if path == "/api/status":
                return self.respond(200, self.server.store.status())
            if path == "/api/dashboard":
                return self.respond(200, self.server.store.dashboard())
            if path == "/api/docker":
                return self.respond(200, self.server.docker.snapshot(force=urlsplit(self.path).query == "refresh=1"))
            if path == "/api/export":
                return self.respond(200, self.server.store.draft_config(), headers={"Content-Disposition": 'attachment; filename="gateway-config.json"'})
            if path == "/api/active.conf":
                return self.respond(200, (self.server.store.active / "nginx.conf").read_text(), "text/plain; charset=utf-8")
            self.respond(404, {"error": "Не найдено"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"admin error: {type(exc).__name__}: {exc}", flush=True)
            self.respond(500, {"error": "Ошибка сервера; проверьте логи контейнера"})

    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            if (origin and urlsplit(origin).netloc != self.headers.get("Host")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.respond(403, {"error": "Cross-origin запрос запрещён"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.respond(415, {"error": "Ожидается application/json"})
            if self.headers.get("Transfer-Encoding"):
                return self.respond(400, {"error": "Transfer-Encoding не поддерживается"})
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1048576:
                return self.respond(413, {"error": "Максимальный размер запроса: 1 MiB"})
            path = urlsplit(self.path).path
            session = self.server.auth.session(self.token())
            if path != "/api/login":
                if not session:
                    return self.respond(401, {"error": "Войдите в панель управления"})
                if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                    return self.respond(403, {"error": "Неверный CSRF-токен. Обновите страницу."})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Ожидается JSON-объект")
            if path == "/api/login":
                result = self.server.auth.login(body.get("password"), self.client_address[0])
                if result is None:
                    return self.respond(401, {"error": "Неверный пароль"})
                token, session = result
                return self.respond(200, {"csrf": session["csrf"]}, headers={"Set-Cookie": self.cookie(token)})
            if path == "/api/logout":
                self.server.auth.logout(self.token())
                return self.respond(200, {"ok": True}, headers={"Set-Cookie": self.cookie("", 0)})
            if path == "/api/password":
                self.server.auth.change_password(body.get("current"), body.get("password"))
                return self.respond(200, {"ok": True}, headers={"Set-Cookie": self.cookie("", 0)})
            if path == "/api/docker":
                return self.respond(200, self.server.docker.save(body["settings"], body["revision"]))
            if path == "/api/config":
                return self.respond(200, self.server.store.save(body["config"], body["revision"]))
            if path == "/api/preview":
                return self.respond(200, {"nginx": self.server.store.preview(body["config"])})
            if path == "/api/test":
                return self.respond(200, {"output": self.server.store.test(body["revision"])})
            if path == "/api/apply":
                return self.respond(200, self.server.store.apply(body["revision"]))
            if path == "/api/restore":
                return self.respond(200, self.server.store.restore(body["generation"], body["revision"]))
            self.respond(404, {"error": "Не найдено"})
        except ConflictError as exc:
            self.respond(409, {"error": str(exc)})
        except PermissionError as exc:
            self.respond(429, {"error": str(exc)}, headers={"Retry-After": "300"})
        except (ValueError, KeyError, TypeError) as exc:
            self.respond(400, {"error": str(exc)})
        except ApplyError as exc:
            self.respond(422, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"admin error: {type(exc).__name__}: {exc}", flush=True)
            self.respond(500, {"error": "Ошибка сервера; проверьте логи контейнера"})
