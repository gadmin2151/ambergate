import copy
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from ambergate.agents import Agents, AgentUnauthorized, inventory, agent_routes
from ambergate.labels import reconcile, discover
from ambergate.tunnel import WebSocket, server_url, MAX_FRAME
from ambergate.tunnel_hub import TunnelHub
from ambergate.config import validate
from .helpers import config
from .test_labels import snapshot


def report(port=8080, identity='a', name='app', label=None):
    return dict(version=1, connected=True, truncated=False, engine_version='29.0', message='', containers=[dict(
        id=identity * 64, name=name, image='test:latest', state='running', status='Up', project='test', service='app',
        networks=['private'], shared_networks=['private'], ports=[port], is_gateway=False,
        endpoints=[dict(address='127.0.0.1', kind='ip')],
        route_labels={'ambergate.route': label or f'host=remote.test;path=/api;port={port};strip=true'})])


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000
        self.hub = TunnelHub(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.hub.stop)
        self.agents = Agents(self.root, self.hub, clock=lambda: self.now)

    def create(self):
        result = self.agents.mutate(dict(action='create', name='Remote Docker', revision=self.agents.revision()))
        control = Mock(closed=threading.Event())
        self.hub.attach(result['agent_id'], control)
        return result['agent_id'], 'Bearer ' + result['token']

    def test_identity_revocation_hashes_redaction_and_persistence(self):
        aid, auth = self.create()
        self.assertEqual(self.agents.authenticate(auth), aid)
        self.assertNotIn(auth[7:], self.agents.file.read_text())
        self.assertNotIn('token_hash', json.dumps(self.agents.snapshot()))
        self.assertEqual(self.agents.file.stat().st_mode & 0o777, 0o600)
        restored = Agents(self.root, self.hub)
        self.assertEqual(restored.authenticate(auth), aid)
        rotated = self.agents.mutate(dict(action='rotate', id=aid, revision=self.agents.revision()))
        with self.assertRaises(AgentUnauthorized):
            self.agents.authenticate(auth)
        self.assertFalse(self.hub.connected(aid))
        auth = 'Bearer ' + rotated['token']
        self.agents.mutate(dict(action='update', id=aid, name='Disabled', enabled=False, revision=self.agents.revision()))
        with self.assertRaises(AgentUnauthorized):
            self.agents.authenticate(auth)

    def test_lease_last_good_and_target_ownership(self):
        aid, auth = self.create()
        self.assertTrue(self.agents.report(auth, report())['ok'])
        entries, warnings, configured = self.agents.routing()
        self.assertTrue(configured)
        self.assertFalse(warnings)
        self.assertEqual(entries[0]['source'], 'agent_' + aid)
        self.assertEqual(entries[0]['target']['address'], '127.0.0.1')
        port = entries[0]['target']['port']
        self.now += 5
        bad = report(); bad['connected'] = False
        self.assertFalse(self.agents.report(auth, bad)['ok'])
        self.assertEqual(self.agents.routing()[0], entries)
        self.now += 30
        self.assertEqual(self.agents.routing()[0], [])
        self.assertEqual(self.hub.destinations[aid], {})
        self.now += 1
        self.assertTrue(self.agents.report(auth, report(identity='b'))['ok'])
        self.assertEqual(self.agents.routing()[0][0]['target']['port'], port)
        self.agents.mutate(dict(action='delete', id=aid, revision=self.agents.revision()))
        self.assertEqual(self.agents.routing(), ([], [], True))
        self.assertEqual(len(self.hub.ports), 1)  # Tombstones never point old history at another app.

    def test_bad_inventory_and_private_network_only(self):
        for change in ({'mounts': []}, {'endpoints': [{'address': 'host', 'kind': 'published'}]},
                       {'id': 'bad'}, {'route_labels': {'secret': 'password'}}):
            raw = report(); raw['containers'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                inventory(raw)
        raw = report(); raw['containers'].append(copy.deepcopy(raw['containers'][0]))
        with self.assertRaises(ValueError): inventory(raw)
        raw = report(label='host=remote.test;port=8080;via=host')
        self.assertTrue(agent_routes(raw)[1])
        raw = report(); raw['containers'][0]['endpoints'] = []
        self.assertTrue(agent_routes(raw)[1])
        aid, auth = self.create()
        raw = report(); raw['truncated'] = True
        self.assertFalse(self.agents.report(auth, raw)['ok'])
        self.assertEqual(self.agents.routing()[0], [])
        with self.assertRaises(PermissionError): self.agents.report(auth, report())

    def test_manual_targets_without_labels_persist_and_follow_container_recreation(self):
        aid, auth = self.create()
        raw = report(); raw['containers'][0]['route_labels'] = {}; raw['containers'][0]['ports'] = []
        self.agents.report(auth, raw, manual_routing=True)
        body = dict(agent_id=aid, targets=[dict(container_id='a'*64, port=9001)])
        target = self.agents.select_targets(body)['targets'][0]
        self.assertEqual(target['agent'], dict(id=aid, container='app', port=9001))
        self.assertEqual(self.agents.routing()[0], [])  # Independent of label automation.
        self.assertEqual(list(self.hub.destinations[aid].values()), ['docker:'+'a'*64+':9001'])
        self.assertEqual(self.agents.select_targets(body)['targets'], [target])
        self.assertEqual(len(self.agents.manual), 1)
        self.assertEqual(self.agents.manual_file.stat().st_mode & 0o777, 0o600)
        self.agents = Agents(self.root, self.hub, clock=lambda: self.now)
        self.now += 5; raw['containers'][0]['id'] = 'b'*64
        self.agents.report(auth, raw, manual_routing=True)
        self.assertEqual(list(self.hub.destinations[aid].values()), ['docker:'+'b'*64+':9001'])
        body['targets'][0]['container_id'] = 'b'*64
        self.assertEqual(self.agents.select_targets(body)['targets'], [target])
        self.now += 5; raw['containers'] = []
        self.agents.report(auth, raw, manual_routing=True)
        self.assertEqual(self.hub.destinations[aid], {})
        self.assertEqual(len(self.hub.ports), 1)

    def test_manual_selection_requires_capability_live_inventory_and_shared_container(self):
        aid, auth = self.create(); raw = report()
        body = dict(agent_id=aid, targets=[dict(container_id='a'*64, port=8080)])
        self.agents.report(auth, raw)
        with self.assertRaisesRegex(ValueError, 'Update the agent'): self.agents.select_targets(body)
        self.now += 5; self.agents.report(auth, raw, manual_routing=True)
        for candidate in (dict(container_id='b'*64, port=8080), dict(container_id='a'*64, port=65536),
                          dict(container_id='a'*64, port=True), dict(container_id='a'*64, port=80, address='localhost')):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.agents.select_targets(dict(agent_id=aid, targets=[candidate]))
        for patch in (dict(is_gateway=True), dict(shared_networks=[]), dict(endpoints=[]), dict(state='exited')):
            altered = copy.deepcopy(raw); altered['containers'][0].update(patch); self.now += 5
            self.agents.report(auth, altered, manual_routing=True)
            with self.subTest(patch=patch), self.assertRaises(ValueError): self.agents.select_targets(body)
        self.now += 5; self.agents.report(auth, raw, manual_routing=True)
        self.now += 31
        with self.assertRaises(ValueError): self.agents.select_targets(body)
        self.agents.routing(); self.assertEqual(self.hub.destinations[aid], {})

    def test_agent_resolves_manual_requests_only_from_its_own_current_inventory(self):
        from ambergate.agent import Agent
        _, auth = self.create()
        raw = report(); raw.pop('version'); raw['containers'][0]['route_labels'] = {}
        docker = Mock(); docker.snapshot.return_value = raw
        agent = Agent('http://localhost:8083', auth[7:], docker, allow_http=True)
        agent.collect()
        self.assertEqual(agent.destination('docker:'+'a'*64+':1234'), ('127.0.0.1',1234))
        for target in ('docker:'+'b'*64+':1234','docker:'+'a'*64+':65536', 'docker:127.0.0.1:80', 'http://localhost'):
            self.assertIsNone(agent.destination(target))
        for patch in (dict(is_gateway=True), dict(shared_networks=[]), dict(endpoints=[]), dict(state='exited')):
            altered=copy.deepcopy(raw);altered['containers'][0].update(patch);docker.snapshot.return_value=altered
            agent.collect();self.assertIsNone(agent.destination('docker:'+'a'*64+':1234'))
        raw['connected']=False;docker.snapshot.return_value=raw
        agent.collect();self.assertIsNone(agent.destination('docker:'+'a'*64+':1234'))

    def test_merge_two_agents_and_freeze_only_local_docker(self):
        a, auth_a = self.create(); b, auth_b = self.create()
        self.agents.report(auth_a, report())
        self.agents.report(auth_b, report())
        entries = self.agents.routing()[0]
        local, _ = discover(snapshot({'ambergate.route': 'host=remote.test;path=/api;port=8080;strip=true'}))
        merged = reconcile(config(), entries + local)
        route = merged['hosts'][-1]['routes'][0]
        self.assertEqual(len(route['docker']['targets']), 3)
        self.assertEqual(set(route['docker']['sources']), {'local', 'agent_' + a, 'agent_' + b})
        after = reconcile(merged, [entries[1]], frozen_sources=('local',))
        self.assertEqual(len(after['hosts'][-1]['routes'][0]['docker']['targets']), 2)
        self.assertEqual(validate(after), after)
        empty = reconcile(after, [])
        self.assertEqual(empty['hosts'][-1]['routes'][0]['docker']['targets'], [])

    def test_claim_is_agent_scoped_one_use_and_not_an_arbitrary_destination(self):
        a, _ = self.create(); b, _ = self.create()
        left, right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        self.hub.pending['request'] = dict(agent=a, claimed=False, done=threading.Event(), ready=threading.Event(), tcp=left, ws=None)
        with self.assertRaises(ValueError): self.hub.claim(b, 'request')
        self.hub.claim(a, 'request')
        with self.assertRaises(ValueError): self.hub.claim(a, 'request')
        with self.assertRaises(ValueError): self.hub.claim(a, 'http://localhost')


class FrameTests(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair()
        server, client = WebSocket(a), WebSocket(b, client=True)
        self.addCleanup(server.close); self.addCleanup(client.close)
        return server, client

    def test_bidirectional_masked_frames_and_ping(self):
        server, client = self.pair()
        for size in (0, 125, 126, 1024, MAX_FRAME):
            payload = b'x' * size
            thread = threading.Thread(target=client.send, args=(payload,)); thread.start()
            self.assertEqual(server.receive(), (2, payload)); thread.join()
        server.send(b'ping', 9); server.send_json({'type': 'open'})
        self.assertEqual(client.receive()[0], 1)
        client.send(b'after pong'); self.assertEqual(server.receive(), (2, b'after pong'))
        client.close()
        with self.assertRaises((EOFError, OSError, ValueError)): server.receive()

    def test_reject_oversize_unmasked_and_fragmented_frames(self):
        for payload in (b'\x82\x00', b'\x02\x80', b'\x82\xff' + struct.pack('!Q', MAX_FRAME + 1)):
            server, client = self.pair(); client.socket.sendall(payload)
            with self.assertRaises(ValueError): server.receive()
            server.close(); client.close()

    def test_require_verified_https_origin(self):
        for url in ('http://example.com', 'https://user:pass@example.com', 'https://example.com/path',
                    'https://example.com?token=secret', 'https://example.com:70000', 'https://example.com\r\n'):
            with self.assertRaises(ValueError): server_url(url)
        self.assertEqual(server_url('https://example.com/').hostname, 'example.com')
        self.assertEqual(server_url('http://localhost:8083', True).port, 8083)
