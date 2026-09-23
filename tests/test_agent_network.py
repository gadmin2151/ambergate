import copy
import unittest
from unittest.mock import Mock, patch
from ambergate.agent_network import NamespaceConnector
from ambergate.docker import DockerError
from ambergate.agents import inventory, wire_snapshot, agent_routes
from .test_agents import report

class NamespaceTests(unittest.TestCase):
    def setUp(self):
        self.info = dict(Id='a'*64, State=dict(Running=True, Pid=1234, StartedAt='2026-09-23T00:00:00Z'), NetworkSettings=dict(Networks={'bridge': {'IPAddress': '172.18.0.2'}}))
        self.docker = Mock(); self.docker.read.return_value = dict(socket_path='/var/run/docker.sock', gateway_container='agent')
        self.docker.get.side_effect = lambda path, endpoint: {'ApiVersion':'1.48'} if endpoint == '/version' else copy.deepcopy(self.info)
        self.connector = NamespaceConnector(self.docker)
    def test_reject_arbitrary_ids_ports_and_agent_itself(self):
        for identity in ('localhost','../1','a'*63,'http://host',123):
            with self.subTest(identity=identity), self.assertRaises(ValueError): self.connector.connect(identity,80)
        self.docker.get.assert_not_called()
        for port in (0,-1,65536,True,'80'):
            with self.subTest(port=port), self.assertRaises(ValueError): self.connector.connect('a'*64,port)
        self.connector.identity='a'*64
        with self.assertRaises(ValueError): self.connector.connect('a'*64,80)
    def test_reject_stopped_mismatched_targets_before_opening_proc(self):
        for state in (dict(Running=False),dict(Pid=0),dict(Pid=True),dict(StartedAt='')):
            old=copy.deepcopy(self.info);self.info['State'].update(state)
            with patch('os.open') as opened, self.assertRaises(DockerError): self.connector.connect('a'*64,80)
            opened.assert_not_called();self.info=old
        self.info['Id']='b'*64
        with self.assertRaises(DockerError): self.connector.connect('a'*64,80)
    def test_pid_reuse_rejected_and_pinned_descriptor_closed(self):
        original=copy.deepcopy(self.info);self.info['State']['StartedAt']='new-start'
        with patch('os.open',return_value=77),patch('os.close') as closed:
            with self.assertRaises(DockerError):self.connector.open_namespace(original)
            closed.assert_called_once_with(77)
    def test_connect_uses_only_docker_addresses_and_closes_fd(self):
        self.info['NetworkSettings']['Networks']['bridge']['GlobalIPv6Address']='fe80::1'
        with patch.object(self.connector,'open_namespace',return_value=88),patch('os.close') as closed,patch('ambergate.agent_network.exchange') as exchange:
            self.connector.connect('a'*64,3000)
            exchange.assert_called_once_with(88,3000,['127.0.0.1','::1','172.18.0.2']);closed.assert_called_once_with(88)
    def test_explicit_namespace_inventory_supports_no_network(self):
        raw=report();raw.pop('version');raw['containers'][0].update(networks=['none'],shared_networks=[],endpoints=[])
        self.assertEqual(wire_snapshot(raw)['containers'][0]['endpoints'],[])
        wire=wire_snapshot(raw,namespace=True)
        self.assertEqual(wire['containers'][0]['endpoints'],[dict(address='a'*64,kind='namespace')])
        self.assertEqual(agent_routes(wire)[0][0]['address'],'a'*64)
        wire['containers'][0]['endpoints'][0]['address']='b'*64
        with self.assertRaises(ValueError):inventory(wire)
        for changes in (dict(is_gateway=True),dict(state='exited')):
            changed=copy.deepcopy(raw);changed['containers'][0].update(changes)
            self.assertEqual(wire_snapshot(changed,namespace=True)['containers'][0]['endpoints'],[])
