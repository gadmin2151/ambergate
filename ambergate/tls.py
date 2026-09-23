"""Read certificate material without exposing private keys to the control plane."""
import hashlib
import json
from pathlib import Path
import re
import ssl


def certificate_name(domain):
    return hashlib.sha256(domain.lower().encode('ascii')).hexdigest()


def inspect_certificate(cert, key, domain):
    info = ssl._ssl._test_decode_cert(str(cert))
    if domain.lower() not in {value.lower() for kind, value in info.get('subjectAltName', ()) if kind == 'DNS'}:
        raise ValueError('Certificate does not match the domain')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return ssl.cert_time_to_seconds(info['notAfter'])


def certificate_info(directory, domain):
    if not directory:
        return None
    root = Path(directory) / certificate_name(domain)
    try:
        version = json.loads((root / 'current.json').read_text())['version']
        if not isinstance(version, str) or not re.fullmatch(r'[a-f0-9]{64}', version):
            return None
        cert, key = root / version / 'fullchain.pem', root / version / 'privkey.pem'
        expires = inspect_certificate(cert, key, domain)
        return dict(certificate=str(cert), key=str(key), expires_at=expires, version=version)
    except (OSError, ValueError, KeyError, TypeError, ssl.SSLError):
        return None
