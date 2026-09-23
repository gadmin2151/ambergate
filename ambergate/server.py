"""Small private control plane; only nginx handles gateway traffic."""
from http.cookies import SimpleCookie, CookieError
from .environment import setting
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import io
import time
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .storage import ApplyError, ConflictError
from .docker import Docker
from .labels import LabelController
from .agents import Agents, AgentUnauthorized
from .tunnel_hub import TunnelHub
from .tunnel import upgrade
from .settings import GeneralSettings, HttpConfirmationRequired
from .http_security import Admission, DeadlineReader, TrustedProxies
from .events import DashboardEvents, event
from .certificates import Certificates

STATIC = Path(__file__).parent / "static"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, store, auth):
        self.store = store
        self.auth = auth
        self.trusted_proxies = TrustedProxies(setting("ADMIN_TRUSTED_PROXIES", ""))
        self.docker = Docker(store.data)
        self.settings = GeneralSettings(store.data)
        configs = [store.draft, *store.revisions.glob("*/config.json")] if hasattr(store, "revisions") else None
        self.tunnels = TunnelHub(store.data, config_paths=configs)
        self.agents = Agents(store.data, self.tunnels)
        self.labels = LabelController(store, self.docker, self.agents)
        self.certificates = Certificates(store)
        self.events = DashboardEvents(self.dashboard)
        self.admission = Admission(self.trusted_proxies)
        try:
            super().__init__(address, Handler)
        except BaseException:
            self.tunnels.stop()
            raise

    def dashboard(self):
        return {**self.store.dashboard(), "labels": self.labels.snapshot(), "agents": self.agents.snapshot(), "general": self.settings.snapshot(), "certificates": self.certificates.snapshot()}

    def serve_forever(self, poll_interval=.5):
        self.tunnels.start()
        self.labels.start()
        self.certificates.start()
        try:
            super().serve_forever(poll_interval)
        finally:
            self.labels.stop()
            self.certificates.stop()

    def shutdown(self):
        self.certificates.stop()
        self.labels.stop()
        self.tunnels.stop()
        self.events.stop()
        super().shutdown()

    def server_close(self):
        self.certificates.stop()
        self.labels.stop()
        self.tunnels.stop()
        self.events.stop()
        super().server_close()

    def process_request(self, request, client_address):
        if not self.admission.enter(request, client_address[0]):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.admission.leave(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.admission.leave(request)


class Handler(BaseHTTPRequestHandler):
    server_version = "AmberGate"
    sys_version = ""

    header_timeout = 10
    body_timeout = 30

    def setup(self):
        super().setup()
        self.rfile.close()
        started = self.server.admission.requests[self.connection][2]
        self.reader = DeadlineReader(self.connection, started + self.header_timeout)
        self.rfile = io.BufferedReader(self.reader)
        self.connection.settimeout(15)

    def parse_request(self):
        parsed = super().parse_request()
        if parsed:
            self.reader.deadline = None
            self.connection.settimeout(15)
            self.client_identity = self.server.trusted_proxies.client(self.client_address[0], self.headers)
            if not self.server.admission.identify(self.connection, self.client_identity):
                self.respond(503, {"error": "Too many pending requests from this client."}, headers={"Retry-After": "1"})
                return False
        return parsed

    def admit(self, kind):
        if self.server.admission.promote(self.connection, kind):
            return True
        self.respond(503, {"error": "Control plane is busy. Retry shortly."}, headers={"Retry-After": "1"})
        return False

    def log_message(self, fmt, *args):
        # Request bodies/passwords and query strings never enter admin logs.
        print(f"admin {self.client_address[0]} {getattr(self, 'command', '-')} {urlsplit(getattr(self, 'path', '')).path} {args[1] if len(args) > 1 else ''}", flush=True)

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
            return cookie["ambergate_session"].value if "ambergate_session" in cookie else ""
        except CookieError:
            return ""

    def cookie(self, token, age=43200):
        secure = "; Secure" if setting("SECURE_COOKIE", "false").lower() == "true" else ""
        return f"ambergate_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}{secure}"

    def agent_tunnel(self, path):
        authorization = self.headers.get("Authorization", "")
        ws = None
        try:
            # Attach/claim under the identity lock so revocation cannot race an
            # authenticated connection into existence after it was disconnected.
            with self.server.agents.lock:
                identity = self.server.agents.authenticate(authorization)
                if not self.admit("agent"):
                    return
                if path == "/api/agents/connect":
                    ws = upgrade(self)
                    self.server.tunnels.attach(identity, ws)
                    pending = None
                else:
                    pending = self.server.tunnels.claim(identity, path.removeprefix("/api/agents/tunnel/"))
                    try:
                        ws = upgrade(self)
                    except BaseException:
                        self.server.tunnels.finish(pending)
                        raise
            if pending is None:
                self.server.tunnels.control(identity, ws)
            else:
                self.server.tunnels.data(pending, ws)
        except AgentUnauthorized as exc:
            if ws is None:
                self.respond(401, {"error": str(exc)})
        except (ValueError, OSError) as exc:
            if ws is None:
                self.respond(400, {"error": str(exc)})
        finally:
            if ws:
                ws.close()

    def stream_dashboard(self):
        # SSE has a separate cap, leaving slots for tunnels and admin requests.
        events = self.server.events
        if not events.subscribe():
            return self.respond(503, {"error": "Слишком много live-подключений"}, headers={"Retry-After": "3"})
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n: connected\n\n")
            self.wfile.flush()
            token, sequence = self.token(), -1
            while not events.stopped:
                sequence, frame = events.wait(sequence)
                if events.stopped:
                    break
                # Revoked/expired sessions must not retain a live data stream.
                if not self.server.auth.session(token):
                    self.wfile.write(event("auth_required", {"error": "Сессия завершена"}))
                    self.wfile.flush()
                    break
                self.wfile.write(frame or b": keepalive\n\n")
                self.wfile.flush()
        except (OSError, TimeoutError):
            pass  # Disconnected/slow readers release their slot; no unbounded queues.
        finally:
            events.unsubscribe()

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == "/api/agents/connect" or path.startswith("/api/agents/tunnel/"):
                return self.agent_tunnel(path)
            if path == "/healthz":
                healthy = self.server.store.status()["healthy"]
                return self.respond(200 if healthy else 503, {"healthy": healthy})
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                     "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                     "/docker.js": ("docker.js", "text/javascript; charset=utf-8"),
                     "/agents.js": ("agents.js", "text/javascript; charset=utf-8"),
                     "/general.js": ("general.js", "text/javascript; charset=utf-8"),
                     "/docker.css": ("docker.css", "text/css; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8"),
                     "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path in files:
                name, mime = files[path]
                return self.respond(200, (STATIC / name).read_bytes(), mime)
            session = self.server.auth.session(self.token())
            if not session:
                return self.respond(401, {"error": "Войдите в панель управления"})
            if not self.admit("admin"):
                return
            if path == "/api/session":
                return self.respond(200, {"csrf": session["csrf"]})
            if path == "/api/config":
                return self.respond(200, self.server.store.snapshot())
            if path == "/api/status":
                return self.respond(200, self.server.store.status())
            if path == "/api/dashboard":
                return self.respond(200, self.server.dashboard())
            if path == "/api/events":
                return self.stream_dashboard()
            if path == "/api/docker":
                return self.respond(200, self.server.docker.snapshot(force=urlsplit(self.path).query == "refresh=1"))
            if path == "/api/docker/labels":
                return self.respond(200, self.server.labels.snapshot())
            if path == "/api/settings":
                return self.respond(200, self.server.settings.snapshot())
            if path == "/api/agents" or path.startswith("/api/agents/"):
                return self.respond(200, self.server.agents.snapshot(None if path == "/api/agents" else path.rsplit("/", 1)[-1]))
            if path == "/api/export":
                return self.respond(200, self.server.store.draft_config(), headers={"Content-Disposition": 'attachment; filename="ambergate-config.json"'})
            if path == "/api/active.conf":
                return self.respond(200, (self.server.store.active / "nginx.conf").read_text(), "text/plain; charset=utf-8")
            self.respond(404, {"error": "Не найдено"})
        except TimeoutError:
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError, TypeError) as exc:
            self.respond(400, {"error": str(exc)})
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
            if path == "/api/agents/report":
                self.server.agents.authenticate(self.headers.get("Authorization", ""))
                if not self.admit("report"):
                    return
            elif path != "/api/login":
                if not session:
                    return self.respond(401, {"error": "Войдите в панель управления"})
                if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                    return self.respond(403, {"error": "Неверный CSRF-токен. Обновите страницу."})
                if not self.admit("admin"):
                    return
            self.reader.deadline = time.monotonic() + self.body_timeout
            try:
                body = json.loads(self.rfile.read(size))
            finally:
                self.reader.deadline = None
                self.connection.settimeout(15)
            if not isinstance(body, dict):
                raise ValueError("Ожидается JSON-объект")
            if path == "/api/agents/report":
                return self.respond(200, self.server.agents.report(self.headers.get("Authorization", ""), body,
                                    manual_routing=self.headers.get("X-AmberGate-Manual-Routing") == "1"))
            if path == "/api/agents/targets":
                return self.respond(200, self.server.agents.select_targets(body))
            if path == "/api/agents":
                return self.respond(200, self.server.agents.mutate(body))
            if path == "/api/settings":
                return self.respond(200, self.server.settings.save(body["settings"], body["revision"], body.get("confirm_http", False)))
            if path == "/api/login":
                result = self.server.auth.login(body.get("password"), self.client_identity)
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
            if path == "/api/docker/labels":
                action = body.get("action")
                if action == "settings":
                    return self.respond(200, self.server.labels.save(body["mode"], body["revision"]))
                if action == "scan":
                    return self.respond(200, self.server.labels.scan())
                if action == "apply" and isinstance(body.get("token"), str) and body["token"]:
                    return self.respond(200, self.server.labels.scan(apply=True, token=body["token"]))
                raise ValueError("Expected label settings, scan or apply with a preview token")
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
        except AgentUnauthorized as exc:
            self.respond(401, {"error": str(exc)})
        except HttpConfirmationRequired as exc:
            self.respond(400, {"error": str(exc), "code": "http_confirmation_required", "public_url": exc.public_url})
        except ConflictError as exc:
            self.respond(409, {"error": str(exc)})
        except PermissionError as exc:
            self.respond(429, {"error": str(exc)}, headers={"Retry-After": "1" if urlsplit(self.path).path == "/api/agents/report" else str(getattr(exc, "retry_after", 300))})
        except (ValueError, KeyError, TypeError) as exc:
            self.respond(400, {"error": str(exc)})
        except ApplyError as exc:
            self.respond(422, {"error": str(exc)})
        except TimeoutError:
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"admin error: {type(exc).__name__}: {exc}", flush=True)
            self.respond(500, {"error": "Ошибка сервера; проверьте логи контейнера"})
