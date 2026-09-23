"""Opt-in HTTP-01 issuance, bounded retries and verified Nginx certificate reloads."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from .environment import setting
from .nginx import digest
from .storage import atomic_write, write_json
from .tls import certificate_info, certificate_name, inspect_certificate


class Certificates:
    def __init__(self, store):
        self.store = store
        self.directory = store.data / 'tls'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.state_file = self.directory / 'status.json'
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        for status in self.state.values():
            status['issuing'] = False
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = None
        self.webroot = Path(store.options['run_dir'] + '-acme')
        self.webroot.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.acme = store.data / 'letsencrypt'
        self.acme.mkdir(mode=0o700, exist_ok=True)

    def start(self):
        if not self.thread:
            self.thread = threading.Thread(target=self.run, name='ambergate-certificates', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def update(self, domain, **fields):
        with self.lock:
            self.state[domain] = {**self.state.get(domain, {}), **fields}
            write_json(self.state_file, self.state)

    def snapshot(self):
        with self.store.lock:
            hosts = self.store.active_config()['hosts']
        with self.lock:
            state = {k: dict(v) for k, v in self.state.items()}
        result = {}
        for host in hosts:
            domain = host['domain'].lower()
            tls = host.get('tls', {})
            enabled = host['enabled'] and tls.get('enabled', False)
            cert = certificate_info(self.directory, domain) if enabled else None
            status = state.get(domain, {}) if enabled else {}
            result[host['id']] = dict(domain=domain, enabled=enabled,
                status=('disabled' if not enabled else 'issuing' if status.get('issuing') else
                        'error' if status.get('error') else 'expired' if cert and cert['expires_at'] <= time.time() else
                        'ready' if cert else 'pending'),
                expires_at=cert['expires_at'] if cert else None,
                renew_at=cert['expires_at'] - tls.get('renew_before_days', 5) * 86400 if cert else None,
                last_attempt=status.get('last_attempt'), next_attempt=status.get('next_attempt'),
                error=status.get('error'))
        return result

    def run(self):
        # Nginx must be serving HTTP-01 before Certbot is started. Only the
        # applied configuration participates; drafts never contact the CA.
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f'certificate worker: {type(exc).__name__}: {exc}', flush=True)
            self.stopping.wait(30)

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.store.lock:
            hosts = self.store.active_config()['hosts']
        for host in hosts:
            if self.stopping.is_set():
                return
            tls = host.get('tls', {})
            if not host['enabled'] or not tls.get('enabled'):
                continue
            domain = host['domain'].lower()
            cert = certificate_info(self.directory, domain)
            fingerprint = digest([domain, tls])
            with self.lock:
                previous = dict(self.state.get(domain, {}))
            if previous.get('fingerprint') == fingerprint and now < previous.get('next_attempt', 0):
                continue
            if cert and now < cert['expires_at'] - tls['renew_before_days'] * 86400:
                continue
            self.update(domain, issuing=True, error=None, last_attempt=now, fingerprint=fingerprint)
            try:
                self.issue(host)
                if self.stopping.is_set():
                    return
                self.update(domain, issuing=False, error=None, failures=0, next_attempt=now + 3600)
            except Exception as exc:
                failures = previous.get('failures', 0) + 1 if previous.get('fingerprint') == fingerprint else 1
                self.update(domain, issuing=False, error=str(exc)[-2000:], failures=failures,
                            next_attempt=now + min(21600, 300 * 2 ** min(failures - 1, 7)))

    def command(self, host):
        domain = host['domain'].lower()
        return [setting('CERTBOT_BIN', 'certbot'), 'certonly', '--non-interactive', '--agree-tos',
                '--email', host['tls']['email'], '--webroot', '--webroot-path', str(self.webroot),
                '--preferred-challenges', 'http', '--cert-name', certificate_name(domain), '-d', domain,
                '--config-dir', str(self.acme), '--work-dir', str(self.acme / 'work'),
                '--logs-dir', str(self.acme / 'logs'), '--key-type', 'ecdsa', '--force-renewal',
                '--server', setting('ACME_SERVER', 'https://acme-v02.api.letsencrypt.org/directory')]

    def invoke(self, command):
        # A log file bounds memory; stop cancels issuance instead of leaving a
        # child process alive while the gateway is shutting down.
        log = self.acme / 'last-command.log'
        with log.open('w+') as output:
            os.chmod(log, 0o600)
            with subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT) as process:
                deadline = time.monotonic() + 180
                while process.poll() is None:
                    if self.stopping.wait(.2) or time.monotonic() >= deadline:
                        process.terminate()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise RuntimeError('Certificate request stopped or timed out')
                if process.returncode:
                    output.seek(max(0, output.tell() - 2000))
                    raise RuntimeError(output.read().strip() or 'Certbot failed; check DNS and public port 80')

    def issue(self, host):
        domain = host['domain'].lower()
        name = certificate_name(domain)
        source = self.acme / 'live' / name
        # Recover a previously issued certificate after an interrupted reload
        # without creating another ACME order.
        reusable = False
        try:
            expires = inspect_certificate(source / 'fullchain.pem', source / 'privkey.pem', domain)
            reusable = expires > time.time() + host['tls']['renew_before_days'] * 86400
        except (OSError, ValueError):
            pass
        if not reusable:
            self.invoke(self.command(host))
        if self.stopping.is_set():
            return
        self.install(host, source)

    def install(self, host, source):
        domain = host['domain'].lower()
        expires = inspect_certificate(source / 'fullchain.pem', source / 'privkey.pem', domain)
        if expires <= time.time():
            raise ValueError('The issued certificate has already expired')
        cert = (source / 'fullchain.pem').read_text()
        key = (source / 'privkey.pem').read_text()
        version = hashlib.sha256((cert + key).encode()).hexdigest()
        root = self.directory / certificate_name(domain)
        target = root / version
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(target / 'fullchain.pem', cert)
        atomic_write(target / 'privkey.pem', key)
        # Immutable certificate paths keep a failed reload rollback valid.
        pointer = root / 'current.json'
        with self.store.lock:
            active = self.store.active_config()
            current = next((h for h in active['hosts'] if h['id'] == host['id']), None)
            if self.stopping.is_set() or not current or not current['enabled'] or not current.get('tls', {}).get('enabled') or current['domain'].lower() != domain or current.get('tls') != host['tls']:
                return
            old = pointer.read_text() if pointer.exists() else None
            try:
                write_json(pointer, dict(version=version))
                self.store.apply_config(active)
            except Exception:
                if old is None:
                    pointer.unlink(missing_ok=True)
                else:
                    atomic_write(pointer, old)
                raise
        # Certificate versions are intentionally retained with config history.
        # They contain secrets and are never part of configuration exports.
