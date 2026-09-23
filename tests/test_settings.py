from pathlib import Path
import tempfile
import unittest

from ambergate.settings import GeneralSettings, HttpConfirmationRequired, public_address
from ambergate.storage import ConflictError


class SettingsTests(unittest.TestCase):
    def test_domains_use_https_and_plain_ips_use_direct_admin_http(self):
        cases = {
            'ambergate.exemple.com': 'https://ambergate.exemple.com',
            ' AMbergate.Exemple.com:8443 ': 'https://ambergate.exemple.com:8443',
            'https://ambergate.exemple.com:443/': 'https://ambergate.exemple.com',
            '10.169.2.14': 'http://10.169.2.14:8083',
            '10.169.2.14:9000': 'http://10.169.2.14:9000',
            '10.169.2.14:80': 'http://10.169.2.14',
            'http://10.169.2.14/': 'http://10.169.2.14',
            'https://10.169.2.14': 'https://10.169.2.14',
            'fd00::1': 'http://[fd00::1]:8083',
            '[fd00::1]': 'http://[fd00::1]:8083',
            '[fd00::1]:9000': 'http://[fd00::1]:9000',
            'https://[fd00::1]:443': 'https://[fd00::1]',
            '': '',
        }
        for value, expected in cases.items():
            with self.subTest(value=value): self.assertEqual(public_address(value), expected)

    def test_reject_non_origins_injection_and_invalid_ports(self):
        for value in ('https://a/api','https://user:pass@a','https://@a','https://a#','https://a?',
                      'ftp://a','a:0','a:65536','a:', 'a b','a\nb','a\\b','-host.example','a..b',
                      'https://$(date).com', 'https://a;echo.com','http://127.000.0.1','https://[fe80::1%eth0]',
                      '//a', 'https://[', 'x'*514, True, None):
            with self.subTest(value=value), self.assertRaises(ValueError): public_address(value)

    def test_http_requires_explicit_consent_bound_to_address_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = GeneralSettings(Path(tmp)); original = settings.snapshot()
            with self.assertRaises(HttpConfirmationRequired) as raised:
                settings.save({'public_url':'10.169.2.14'}, original['revision'])
            self.assertEqual(raised.exception.public_url,'http://10.169.2.14:8083')
            self.assertEqual(settings.snapshot(), original)
            self.assertFalse(settings.file.exists())
            allowed=settings.save({'public_url':'10.169.2.14'}, original['revision'],True)
            self.assertEqual(allowed['settings'],dict(public_url='http://10.169.2.14:8083',allow_http=True))
            self.assertEqual(settings.file.stat().st_mode&0o777,0o600)
            settings=GeneralSettings(Path(tmp));self.assertEqual(settings.snapshot(),allowed)
            self.assertEqual(settings.save({'public_url':'10.169.2.14'},allowed['revision']),allowed)
            with self.assertRaises(HttpConfirmationRequired):settings.save({'public_url':'10.169.2.15'},allowed['revision'])
            secure=settings.save({'public_url':'ambergate.exemple.com'},allowed['revision'])
            self.assertFalse(secure['settings']['allow_http'])
            with self.assertRaises(ConflictError):settings.save({'public_url':'10.169.2.14'},allowed['revision'],True)
            with self.assertRaises(HttpConfirmationRequired):settings.save({'public_url':'10.169.2.14'},secure['revision'])
            with self.assertRaises(ValueError):settings.save({'public_url':'10.169.2.14'},secure['revision'],'true')
