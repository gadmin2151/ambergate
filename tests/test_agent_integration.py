"""Real Nginx -> loopback tunnel -> outbound agent -> HTTP/WebSocket application."""
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from ambergate.agent import Agent
from ambergate.auth import Auth
from ambergate.server import Server
from ambergate.storage import Store
from ambergate.tunnel import connect, upgrade
from .test_agents import report
from .test_integration import NGINX, MIME, free_port, request
from .helpers import config


class App(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            ws = upgrade(self)
            try:
                opcode, payload = ws.receive()
                ws.send(payload, opcode)
            finally:
                ws.close()
            return
        if self.path == '/stream':
            self.send_response(200); self.send_header('Content-Length', '6'); self.send_header('Content-Type','text/event-stream'); self.send_header('X-Accel-Buffering','no'); self.end_headers()
            self.wfile.write(b'first'); self.wfile.flush()
            self.server.first.set()
            self.server.release.wait(4)
            self.wfile.write(b'!'); self.wfile.flush()
            return
        data = json.dumps(dict(app=self.server.server_port, path=self.path)).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        value = self.rfile.read(int(self.headers['Content-Length']))
        data = hashlib.sha256(value).hexdigest().encode()
        self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_): pass


def eventually(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result: return result
        time.sleep(.1)
    raise AssertionError('Timed out waiting for agent state')


@unittest.skipUnless(NGINX and Path(MIME).is_file(), 'Requires real Nginx')
class AgentIntegrationTests(unittest.TestCase):
    def test_end_to_end_balancing_upload_websocket_isolation_expiry_and_restart(self):
        with tempfile.TemporaryDirectory(prefix='agent-integration-') as tmp:
            root = Path(tmp); root.chmod(0o755)
            store = Store(root/'data', root/'cache', root/'run', NGINX, port=free_port(), control_port=free_port(), mime_types=MIME)
            store.start()
            self.addCleanup(store.stop)
            with patch.dict(os.environ, {'AMBERGATE_ADMIN_PASSWORD': 'agent-test-password-123'}): auth = Auth(store.data)
            admin = Server(('127.0.0.1', 0), store, auth)
            threading.Thread(target=admin.serve_forever, daemon=True).start()
            self.addCleanup(admin.server_close); self.addCleanup(admin.shutdown)
            origin = f'http://127.0.0.1:{admin.server_port}'
            app_servers, agents, identities, tokens = [], [], [], []
            try:
                for index in range(2):
                    app = ThreadingHTTPServer(('127.0.0.1', 0), App)
                    app.first, app.release = threading.Event(), threading.Event()
                    threading.Thread(target=app.serve_forever, daemon=True).start()
                    app_servers.append(app)
                    result = admin.agents.mutate(dict(action='create', name=f'Agent {index}', timeout=15, revision=admin.agents.revision()))
                    identities.append(result['agent_id']); tokens.append(result['token'])
                    raw = report(app.server_port, identity='ab'[index]); raw.pop('version')
                    docker = Mock(); docker.snapshot.return_value = raw
                    agent = Agent(origin, result['token'], docker, allow_http=True, interval=2)
                    threading.Thread(target=agent.run, daemon=True).start(); agents.append(agent)
                eventually(lambda: len(admin.agents.routing()[0]) == 2)
                admin.labels.save('preview', admin.labels.snapshot()['revision'])
                outcome = admin.labels.scan(apply=True)
                self.assertFalse(outcome['errors'], outcome)
                active = store.active_config()
                remote = next(h for h in active['hosts'] if h['domain'] == 'remote.test')['routes'][0]
                self.assertEqual(len(remote['docker']['targets']), 2)
                gateway = store.options["port"]
                seen = set()
                for _ in range(12):
                    status, _, body = request(gateway, '/api/hello?x=1', headers={'Host': 'remote.test'})
                    self.assertEqual(status, 200, body)
                    data = json.loads(body); seen.add(data['app']); self.assertEqual(data['path'], '/hello?x=1')
                self.assertEqual(seen, {a.server_port for a in app_servers})
                payload = os.urandom(350000)
                status, _, body = request(gateway, '/api/upload', method='POST', headers={'Host': 'remote.test'}, body=payload)
                self.assertEqual(status, 200); self.assertEqual(body.decode(), hashlib.sha256(payload).hexdigest())
                # Raw application WebSocket is carried unchanged inside the private tunnel.
                from ambergate.tunnel import WebSocket
                import base64
                conn = socket.create_connection(('127.0.0.1', gateway), timeout=5)
                reader = conn.makefile('rb'); key = base64.b64encode(os.urandom(16)).decode()
                conn.sendall((f'GET /api/ws HTTP/1.1\r\nHost: remote.test\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n').encode())
                self.assertIn(b'101', reader.readline())
                while reader.readline() != b'\r\n': pass
                ws = WebSocket(conn, reader, client=True)
                ws.send(b'echo\x00\xff'); self.assertEqual(ws.receive(), (2, b'echo\x00\xff')); ws.close()
                # SSE reaches the client before the application completes its response.
                import http.client
                stream = http.client.HTTPConnection('127.0.0.1',gateway,timeout=3)
                stream.request('GET','/api/stream',headers={'Host':'remote.test'})
                response = stream.getresponse()
                self.assertEqual(response.read(5),b'first')
                self.assertTrue(any(app.first.is_set() for app in app_servers))
                for app in app_servers: app.release.set()
                self.assertEqual(response.read(),b'!'); stream.close()
                # Agent bearer token cannot read admin state or create other agents.
                self.assertEqual(request(admin.server_port, '/api/agents', headers={'Authorization': 'Bearer '+tokens[0]})[0], 401)
                self.assertEqual(request(admin.server_port, '/api/agents', method='POST', body='{}', headers={'Content-Type':'application/json','Authorization':'Bearer '+tokens[0]})[0], 401)
                with self.assertRaises(ConnectionError): connect(origin, '/api/agents/tunnel/' + 'a'*32, tokens[1], allow_http=True)
                # Revocation cuts streams immediately, and reconciliation keeps the other replica.
                agents[0].stop()
                admin.agents.mutate(dict(action='delete', id=identities[0], revision=admin.agents.revision()))
                outcome = admin.labels.scan(apply=True); self.assertFalse(outcome['errors'], outcome)
                status, _, body = request(gateway, '/api/after', headers={'Host':'remote.test'})
                self.assertEqual(status, 200); self.assertEqual(json.loads(body)['app'], app_servers[1].server_port)
                # Lease removal cannot resurrect stale targets from an unrelated source.
                agents[1].stop(); eventually(lambda: not admin.tunnels.connected(identities[1]))
                admin.agents.clock = lambda: time.time() + 31
                outcome = admin.labels.scan(apply=True); self.assertFalse(outcome['errors'], outcome)
                self.assertEqual(request(gateway, '/api/after', headers={'Host':'remote.test'})[0], 503)
                admin.agents.clock = time.time
                replacement = Agent(origin, tokens[1], agents[1].docker, allow_http=True, interval=2)
                agents.append(replacement); threading.Thread(target=replacement.run, daemon=True).start()
                eventually(lambda: len(admin.agents.routing()[0]) == 1 and admin.tunnels.connected(identities[1]))
                outcome = admin.labels.scan(apply=True); self.assertFalse(outcome['errors'], outcome)
                self.assertEqual(request(gateway, '/api/recovered', headers={'Host':'remote.test'})[0], 200)
            finally:
                for agent in agents: agent.stop()
                admin.shutdown(); admin.server_close(); store.stop()
                for app in app_servers:
                    app.release.set(); app.shutdown(); app.server_close()
