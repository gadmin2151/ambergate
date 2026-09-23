import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ambergate.auth import Auth
from ambergate.environment import setting


class EnvironmentCompatibilityTests(unittest.TestCase):
    def test_legacy_settings_remain_available(self):
        with patch.dict(os.environ, {'GATEWAY_RUN_DIR':'/run/gateway', 'GATEWAY_SECURE_COOKIE':'true'}, clear=True):
            self.assertEqual(setting('RUN_DIR', '/run/ambergate'), '/run/gateway')
            self.assertEqual(setting('SECURE_COOKIE', 'false'), 'true')
            self.assertEqual(setting('HTTP_PORT', '80'), '80')

    def test_ambergate_settings_take_precedence(self):
        with patch.dict(os.environ, {'AMBERGATE_ADMIN_BIND':'127.0.0.1', 'GATEWAY_ADMIN_BIND':'0.0.0.0', 'AMBERGATE_DOCKER_CONTAINER':'', 'GATEWAY_DOCKER_CONTAINER':'old-name'}, clear=True):
            self.assertEqual(setting('ADMIN_BIND'), '127.0.0.1')
            self.assertEqual(setting('DOCKER_CONTAINER'), '')

    def test_existing_account_is_not_replaced_by_new_environment(self):
        with tempfile.TemporaryDirectory() as root:
            data=Path(root)
            with patch.dict(os.environ, {'GATEWAY_ADMIN_PASSWORD':'original-password-123'}, clear=True):
                self.assertTrue(Auth(data).verify('original-password-123'))
            before=(data/'admin.json').read_bytes()
            with patch.dict(os.environ, {'AMBERGATE_ADMIN_PASSWORD':'different-password-456'}, clear=True):
                self.assertTrue(Auth(data).verify('original-password-123'))
            self.assertEqual((data/'admin.json').read_bytes(), before)
