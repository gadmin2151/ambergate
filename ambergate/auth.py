from .environment import setting
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time

from .storage import write_json


class LoginLimited(PermissionError):
    def __init__(self, retry_after):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__("Too many login attempts. Retry shortly.")


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()


class Auth:
    def __init__(self, data_dir):
        self.path = data_dir / "admin.json"
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = {}
        self.login_slots = threading.BoundedSemaphore(4)
        self.login_sources = set()
        if not self.path.exists():
            password = setting("ADMIN_PASSWORD") or secrets.token_urlsafe(20)
            self.set_password(password)
            if not setting("ADMIN_PASSWORD"):
                print(f"\nAmberGate admin — initial password: {password}\n"
                      "Save it now. It will not be printed again.\n", flush=True)
        self.credentials = json.loads(self.path.read_text())

    def set_password(self, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError("Пароль должен содержать от 12 до 256 символов")
        salt = secrets.token_hex(32)
        credentials = {"salt": salt, "hash": hash_password(password, salt)}
        write_json(self.path, credentials)
        self.credentials = credentials
        self.sessions.clear()

    def verify(self, password):
        return isinstance(password, str) and len(password) <= 256 and hmac.compare_digest(
            self.credentials["hash"], hash_password(password, self.credentials["salt"]))

    def login(self, password, source):
        with self.lock:
            now = time.monotonic()
            self.attempts = {k: v for k, v in self.attempts.items() if now - v[1] < 300}
            credit, previous = self.attempts.get(source, (10, now))
            credit = min(10, credit + (now - previous) / 30)
            if credit < 1:
                raise LoginLimited((1 - credit) * 30)
            if source in self.login_sources or (source not in self.attempts and len(self.attempts) >= 10000):
                raise LoginLimited(1)
            if not self.login_slots.acquire(blocking=False):
                raise LoginLimited(1)
            self.login_sources.add(source)
            self.attempts[source] = (credit - 1, now)
            credentials = self.credentials
        try:
            # Expensive password work cannot block session checks, SSE or logout.
            valid = isinstance(password, str) and len(password) <= 256 and hmac.compare_digest(
                credentials["hash"], hash_password(password, credentials["salt"]))
        finally:
            with self.lock:
                self.login_sources.discard(source)
                self.login_slots.release()
        with self.lock:
            if not valid or credentials is not self.credentials:
                return None
            self.attempts.pop(source, None)
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            if len(self.sessions) >= 100:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            session = {"csrf": secrets.token_urlsafe(32), "expires": now + 12 * 3600}
            self.sessions[token] = session
            return token, session

    def session(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if session and session["expires"] > time.monotonic():
                return dict(session)
            self.sessions.pop(token, None)
            return None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def change_password(self, old, new):
        with self.lock:
            if not self.verify(old):
                raise ValueError("Текущий пароль неверен")
            self.set_password(new)
