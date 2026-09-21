import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from .storage import write_json


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()


class Auth:
    def __init__(self, data_dir):
        self.path = data_dir / "admin.json"
        self.lock = threading.RLock()
        self.sessions = {}
        self.attempts = {}
        if not self.path.exists():
            password = os.environ.get("GATEWAY_ADMIN_PASSWORD") or secrets.token_urlsafe(20)
            self.set_password(password)
            if not os.environ.get("GATEWAY_ADMIN_PASSWORD"):
                print(f"\nGateway admin — initial password: {password}\n"
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
            self.attempts = {k: v for k, v in self.attempts.items() if v[1] > now}
            count, expires = self.attempts.get(source, (0, now + 300))
            if count >= 10 or len(self.attempts) >= 10000:
                raise PermissionError("Слишком много попыток. Повторите через 5 минут.")
            self.attempts[source] = (count + 1, expires)
            if not self.verify(password):
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
