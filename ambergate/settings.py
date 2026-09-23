"""Local control-plane settings, independent of Nginx routing drafts."""
import ipaddress
import json
import re
import threading
from urllib.parse import urlsplit

from .nginx import digest
from .storage import ConflictError, write_json


class HttpConfirmationRequired(ValueError):
    def __init__(self, public_url):
        super().__init__('HTTP sends agent tokens and application traffic without encryption. Confirm the risk to continue.')
        self.public_url = public_url


def public_address(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError('Enter an IP address or domain for AmberGate')
    value = value.strip()
    if not value:
        return ''
    if any(ord(c) <= 32 for c in value) or any(c in value for c in ('\\', '?', '#')):
        raise ValueError('Use an address without a path, credentials, query or fragment')
    explicit = '://' in value
    # Bare IPs are the direct admin listener; a bare domain uses external HTTPS.
    if not explicit:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            candidate = urlsplit('//' + value)
            try:
                ip = ipaddress.ip_address(candidate.hostname or '')
            except ValueError:
                ip = None
            value = ('http://' if ip else 'https://') + value
        else:
            value = 'http://' + (f'[{ip}]' if ip.version == 6 else str(ip))
    try:
        url = urlsplit(value)
        port = url.port
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
                or url.password is not None or url.path not in ('', '/') or url.query or url.fragment
                or url.netloc.endswith(':') or port == 0):
            raise ValueError()
        host = url.hostname
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode('idna').decode('ascii').lower()
            if (len(host) > 253 or re.fullmatch(r'[0-9.]+', host) or
                    not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in host.split('.'))):
                raise ValueError()
        else:
            if '%' in host:
                raise ValueError()
            host = f'[{ip}]' if ip.version == 6 else str(ip)
            if not explicit and port is None:
                port = 8083
        if port == (443 if url.scheme == 'https' else 80):
            port = None
        return f'{url.scheme}://{host}' + (f':{port}' if port else '')
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Use an HTTP or HTTPS domain/IP, with an optional port and no path or credentials') from exc


class GeneralSettings:
    def __init__(self, data):
        self.file = data / 'settings.json'
        self.lock = threading.RLock()
        self.value = json.loads(self.file.read_text()) if self.file.exists() else dict(public_url='', allow_http=False)
        if (not isinstance(self.value, dict) or set(self.value) != {'public_url', 'allow_http'}
                or type(self.value['allow_http']) is not bool
                or public_address(self.value['public_url']) != self.value['public_url']
                or self.value['allow_http'] != self.value['public_url'].startswith('http://')):
            raise ValueError('Invalid general settings file')

    def snapshot(self):
        with self.lock:
            return dict(settings=dict(self.value), revision=digest(self.value))

    def save(self, settings, revision, confirm_http=False):
        if not isinstance(settings, dict) or set(settings) != {'public_url'} or type(confirm_http) is not bool:
            raise ValueError('Invalid general settings')
        address = public_address(settings['public_url'])
        with self.lock:
            if revision != digest(self.value):
                raise ConflictError('General settings changed. Refresh and try again.')
            insecure = address.startswith('http://')
            if insecure and not confirm_http and self.value != dict(public_url=address, allow_http=True):
                raise HttpConfirmationRequired(address)
            value = dict(public_url=address, allow_http=insecure)
            write_json(self.file, value)
            self.value = value
            return self.snapshot()
