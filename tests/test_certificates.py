"""Certificate scheduling/rollback and HTTP-01/HTTPS against actual Nginx."""
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ambergate.certificates import Certificates
from ambergate.config import validate, ValidationError
from ambergate.nginx import render
from ambergate.storage import Store, ApplyError
from ambergate.tls import certificate_info, certificate_name
from .helpers import config
from .test_integration import NGINX, MIME, Upstream, free_port, request
from http.server import ThreadingHTTPServer


def tls_config():
    c = config()
    c['hosts'][0]['tls'] = dict(enabled=True, email='admin@example.com', renew_before_days=5,
                              redirect_http=True, terms_accepted=True)
    return c


def make_cert(directory, domain='gateway.test', days=10):
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                    '-nodes', '-keyout', str(directory/'privkey.pem'), '-out', str(directory/'fullchain.pem'),
                    '-days', str(days), '-subj', '/CN='+domain, '-addext', 'subjectAltName=DNS:'+domain],
                   check=True, capture_output=True)
    return directory


class TLSValidationTests(unittest.TestCase):
    def test_existing_configs_default_off_and_validation_is_strict(self):
        old = config()
        self.assertEqual(validate(old), old)
        self.assertNotIn('ssl_certificate', render(old, 'test'))
        self.assertNotIn('acme-challenge', render(old, 'test'))
        self.assertEqual(validate(tls_config()), tls_config())
        for key, value in [('renew_before_days', 0), ('renew_before_days', 31), ('renew_before_days', True),
                           ('email', ''), ('email', 'foo\nbar@example.com'), ('terms_accepted', False), ('enabled', 1)]:
            c = tls_config(); c['hosts'][0]['tls'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError): validate(c)
        for domain in ['127.0.0.1', 'localhost', '*.example.com']:
            c = tls_config(); c['hosts'][0]['domain'] = domain
            with self.subTest(domain=domain), self.assertRaises(ValidationError): validate(c)


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required')
class CertificateLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.active = tls_config()
        self.store = Mock(data=self.root/'data', options={'run_dir':str(self.root/'run')}, lock=threading.RLock())
        self.store.data.mkdir()
        self.store.active_config.side_effect = lambda: copy.deepcopy(self.active)
        self.manager = Certificates(self.store)
        self.host = self.active['hosts'][0]

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def test_opt_in_and_active_only(self):
        with patch.object(self.manager, 'issue') as issue:
            del self.active['hosts'][0]['tls']
            self.manager.tick(); issue.assert_not_called()
            self.active = tls_config(); self.active['hosts'][0]['enabled'] = False
            self.manager.tick(); issue.assert_not_called()
            self.active['hosts'][0]['enabled'] = True
            self.active['hosts'][0]['tls']['enabled'] = False
            self.manager.tick(); issue.assert_not_called()
            self.active['hosts'][0]['tls']['enabled'] = True
            self.manager.tick(); issue.assert_called_once()

    def test_exact_renewal_boundary_and_persisted_backoff(self):
        self.manager.install(self.host, make_cert(self.root/'source'))
        cert = certificate_info(self.manager.directory, 'gateway.test')
        due = cert['expires_at'] - 5*86400
        with patch.object(self.manager, 'issue', side_effect=RuntimeError('validation failed')) as issue:
            self.manager.tick(due-1); issue.assert_not_called()
            self.manager.tick(due); self.assertEqual(issue.call_count, 1)
            self.manager.tick(due+1); self.assertEqual(issue.call_count, 1)
        restarted = Certificates(self.store)
        with patch.object(restarted, 'issue') as issue:
            restarted.tick(due+299); issue.assert_not_called()
            restarted.tick(due+300); issue.assert_called_once()
        state = restarted.snapshot()['host1']
        self.assertEqual(state['status'], 'ready')
        self.assertNotIn('privkey', json.dumps(state))

    def test_rollback_preserves_previous_certificate_and_disabled_domain_ignores_result(self):
        self.manager.install(self.host, make_cert(self.root/'first'))
        before = certificate_info(self.manager.directory, 'gateway.test')
        self.store.apply_config.side_effect = ApplyError('reload failed')
        with self.assertRaises(ApplyError):
            self.manager.install(self.host, make_cert(self.root/'second'))
        self.assertEqual(certificate_info(self.manager.directory, 'gateway.test'), before)
        self.active['hosts'][0]['tls']['enabled'] = False
        self.store.apply_config.reset_mock()
        self.manager.install(self.host, self.root/'second')
        self.store.apply_config.assert_not_called()
        self.assertEqual(certificate_info(self.manager.directory, 'gateway.test'), before)

    def test_recover_issued_material_without_another_acme_order(self):
        source = self.manager.acme/'live'/certificate_name('gateway.test')
        make_cert(source)
        with patch.object(self.manager, 'invoke') as invoke:
            self.manager.issue(self.host); invoke.assert_not_called()
        self.assertIsNotNone(certificate_info(self.manager.directory, 'gateway.test'))
        with self.assertRaises(ValueError):
            self.manager.install(self.host, make_cert(self.root/'wrong', 'other.test'))

    def test_command_is_webroot_scoped_and_contains_no_nginx_installer(self):
        command=self.manager.command(self.host)
        self.assertIn('certonly', command);self.assertIn('--webroot', command)
        self.assertIn('--force-renewal', command);self.assertNotIn('--nginx', command)
        self.assertEqual(command[command.index('-d')+1], 'gateway.test')


@unittest.skipUnless(NGINX and shutil.which('openssl'), 'Nginx with SSL and OpenSSL required')
class HTTPSIntegrationTests(unittest.TestCase):
    def test_challenge_https_redirect_draft_isolation_and_failed_reload(self):
        version=subprocess.run([NGINX,'-V'],capture_output=True,text=True).stderr
        if '--with-http_ssl_module' not in version:self.skipTest('Nginx has no SSL module')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);root.chmod(0o755)
            backend=ThreadingHTTPServer(('127.0.0.1',0),Upstream);backend.count=0
            threading.Thread(target=backend.serve_forever,daemon=True).start()
            store=Store(root/'data',root/'cache',root/'run',NGINX,port=free_port(),control_port=free_port(),mime_types=MIME,tls_port=free_port())
            manager=Certificates(store)
            try:
                store.start();c=tls_config();c['hosts'][0]['routes'][0]['targets'][0]['port']=backend.server_port
                store.apply_config(c)
                draft=copy.deepcopy(c);draft['settings']['body_mb']=321
                store.save(draft,store.snapshot()['revision'])
                token=manager.webroot/'.well-known/acme-challenge/test-token';token.parent.mkdir(parents=True);token.write_text('challenge-value')
                self.assertEqual(request(store.options['port'],'/.well-known/acme-challenge/test-token')[2],b'challenge-value')
                source=make_cert(root/'issued')
                manager.install(c['hosts'][0],source)
                self.assertEqual(store.active_config(),c)
                self.assertEqual(store.draft_config(),draft)
                self.assertTrue(store.snapshot()['pending'])
                status,headers,_=request(store.options['port'],'/hello?q=1')
                self.assertEqual((status,headers['Location']),(308,'https://gateway.test/hello?q=1'))
                self.assertEqual(request(store.options['port'],'/.well-known/acme-challenge/test-token')[2],b'challenge-value')
                def https():
                    context=ssl.create_default_context(cafile=str(source/'fullchain.pem'))
                    with socket.create_connection(('127.0.0.1',store.options['tls_port'])) as raw:
                        with context.wrap_socket(raw,server_hostname='gateway.test') as sock:
                            sock.sendall(b'GET /hello?q=1 HTTP/1.1\r\nHost: gateway.test\r\nConnection: close\r\n\r\n')
                            response=http.client.HTTPResponse(sock);response.begin()
                            self.assertEqual(response.status,200)
                            data=json.loads(response.read());self.assertEqual(data['path'],'/hello?q=1')
                            self.assertEqual(data['headers']['X-Forwarded-Proto'],'https')
                https()
                old=store.active_id()
                with patch.object(store,'check',side_effect=ApplyError('test failure')):
                    with self.assertRaises(ApplyError):manager.install(c['hosts'][0],make_cert(root/'next'))
                self.assertEqual(store.active_id(),old);https()
                store.stop();store.start();https()
                c['hosts'][0]['tls']['enabled']=False;store.apply_config(c)
                self.assertEqual(request(store.options['port'])[0],200)
                self.assertNotIn('ssl_certificate',(store.active/'nginx.conf').read_text())
            finally:
                manager.stop();store.stop();backend.shutdown();backend.server_close()
