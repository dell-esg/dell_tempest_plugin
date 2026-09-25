# Copyright 2026 Dell Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import shutil
import socket
import subprocess
import tempfile
import time

from tempest.lib import decorators

from dell_tempest_plugin.tests.powerscale_manila.test_powerscale_dedupe import (  # noqa: E501
    PowerScaleDedupeShareTest,
)

ACCESS_TIMEOUT = 300
ACCESS_INTERVAL = 5


class PowerScaleAccessSecurityTest(PowerScaleDedupeShareTest):
    def _create_access_share(self, protocol):
        share_type = self.create_non_dedupe_share_type(
            extra_specs={'share_backend_name': 'powerscale'})
        share = self.create_share(
            protocol=protocol,
            share_type_name=share_type['name'],
        )
        return share

    def _get_export_path(self, share_id):
        locations = self._get_export_locations(share_id)
        self.assertTrue(locations, 'Share must have an export location')
        location = locations[0]
        if isinstance(location, dict):
            location = location.get('path', location)
        return location

    def _get_access_rules(self, share_id):
        response = self.shares_v2_client.list_access_rules(share_id)
        rules = (response.get('access_list') or
                 response.get('share_access_rules') or response)
        return rules if isinstance(rules, list) else []

    def _wait_access_rule(self, share_id, rule_id, expected_state):
        deadline = time.time() + ACCESS_TIMEOUT
        while time.time() < deadline:
            rule = next(
                (item for item in self._get_access_rules(share_id)
                 if item.get('id') == rule_id),
                None,
            )
            state = (rule or {}).get(
                'state', (rule or {}).get('access_state', ''))
            if state.lower() == expected_state:
                return
            if state.lower() == 'error':
                self.fail(
                    f'Access rule {rule_id} entered error state on '
                    f'share {share_id}')
            time.sleep(ACCESS_INTERVAL)
        self.fail(
            f'Timed out waiting for access rule {rule_id} on share '
            f'{share_id} to reach {expected_state}')

    def _wait_access_rule_deleted(self, share_id, rule_id):
        deadline = time.time() + ACCESS_TIMEOUT
        while time.time() < deadline:
            if not any(item.get('id') == rule_id
                       for item in self._get_access_rules(share_id)):
                return
            time.sleep(ACCESS_INTERVAL)
        self.fail(
            f'Timed out waiting for access rule {rule_id} removal from '
            f'share {share_id}')

    @staticmethod
    def _backend_host(export_path):
        if export_path.startswith('\\\\'):
            return export_path[2:].split('\\', 1)[0]
        return export_path.split(':', 1)[0]

    def _local_ip(self, backend_host):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((backend_host, 445))
            return sock.getsockname()[0]
        finally:
            sock.close()

    def _mount_nfs(self, export_path, expected_success):
        mount_dir = tempfile.mkdtemp(prefix='pscale-nfs-access-')
        mounted = False
        try:
            result = subprocess.run(
                ['mount', '-t', 'nfs', '-o', 'vers=3', export_path,
                 mount_dir],
                capture_output=True,
                text=True,
                timeout=30,
            )
            mounted = result.returncode == 0
            if expected_success:
                self.assertEqual(
                    0,
                    result.returncode,
                    result.stderr or result.stdout,
                )
            else:
                self.assertNotEqual(
                    0,
                    result.returncode,
                    'An unprivileged client mounted an unrestricted NFS '
                    'share without an access rule',
                )
        finally:
            if mounted:
                subprocess.run(
                    ['umount', mount_dir],
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
            shutil.rmtree(mount_dir, ignore_errors=True)

    def _smb_credentials_file(self):
        username = os.getenv('POWERSCALE_SMB_USERNAME')
        password = os.getenv('POWERSCALE_SMB_PASSWORD')
        if not username or not password:
            self.skipTest(
                'Set POWERSCALE_SMB_USERNAME and '
                'POWERSCALE_SMB_PASSWORD for SMB integration tests')
        credentials = tempfile.NamedTemporaryFile(
            mode='w', prefix='pscale-smb-', delete=False)
        credentials.write(f'username = {username}\npassword = {password}\n')
        credentials.close()
        os.chmod(credentials.name, 0o600)
        self.addCleanup(self._remove_file, credentials.name)
        return credentials.name

    @staticmethod
    def _remove_file(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def _smb_access(self, export_path, expected_success):
        credentials_file = self._smb_credentials_file()
        unc_path = export_path.replace('\\', '/')
        result = subprocess.run(
            ['smbclient', unc_path, '-A', credentials_file, '-m', 'SMB3',
             '-c', 'ls'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = f'{result.stdout}\n{result.stderr}'
        if expected_success:
            self.assertEqual(0, result.returncode, output)
        else:
            self.assertNotEqual(0, result.returncode)
            self.assertIn('NT_STATUS_ACCESS_DENIED', output)

    def _create_rule(self, share_id, access_to):
        response = self.shares_v2_client.create_access_rule(
            share_id,
            access_type='ip',
            access_to=access_to,
            access_level='rw',
        )
        rule = response.get('access', response)
        self._wait_access_rule(share_id, rule['id'], 'active')
        return rule

    def _delete_rule(self, share_id, rule_id):
        self.shares_v2_client.delete_access_rule(share_id, rule_id)
        self._wait_access_rule_deleted(share_id, rule_id)


class _NFSAccessSecurityTests(object):
    @decorators.idempotent_id('a0c4d7f2-76e9-4f6d-9d0c-1d0efb2c4a10')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_no_rule_denies_mount(self):
        share = self._create_access_share('NFS')
        export_path = self._get_export_path(share['id'])
        self._mount_nfs(export_path, expected_success=False)

    @decorators.idempotent_id('b1d5e8a3-87fa-507e-ae1d-2e1f3c3d5b21')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_final_rule_removal_restores_deny(self):
        share = self._create_access_share('NFS')
        export_path = self._get_export_path(share['id'])
        host = self._backend_host(export_path)
        local_ip = self._local_ip(host)

        self._mount_nfs(export_path, expected_success=False)
        rule = self._create_rule(share['id'], local_ip)
        self._mount_nfs(export_path, expected_success=True)
        self._delete_rule(share['id'], rule['id'])
        self._mount_nfs(export_path, expected_success=False)


class _CIFSAccessSecurityTests(object):
    @decorators.idempotent_id('c2e6f9b4-98ab-618f-bf2e-3f23d4e6c320')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_no_rule_denies_tree_connect(self):
        share = self._create_access_share('CIFS')
        export_path = self._get_export_path(share['id'])
        self._smb_access(export_path, expected_success=False)

    @decorators.idempotent_id('d3f7a0c5-a9bc-7290-cf3f-4a3b5e7f7d43')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_final_rule_removal_restores_deny(self):
        share = self._create_access_share('CIFS')
        export_path = self._get_export_path(share['id'])
        host = self._backend_host(export_path)
        local_ip = self._local_ip(host)

        self._smb_access(export_path, expected_success=False)
        rule = self._create_rule(share['id'], local_ip)
        self._smb_access(export_path, expected_success=True)
        self._delete_rule(share['id'], rule['id'])
        self._smb_access(export_path, expected_success=False)


try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleAccessSecurityNFS(
            _NFSAccessSecurityTests,
            PowerScaleAccessSecurityTest,
            manila_base.BaseSharesAdminTest):
        pass

    class TestPowerScaleAccessSecurityCIFS(
            _CIFSAccessSecurityTests,
            PowerScaleAccessSecurityTest,
            manila_base.BaseSharesAdminTest):
        pass
except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleAccessSecurityNFS(
            _NFSAccessSecurityTests,
            PowerScaleAccessSecurityTest,
            tempest_test.BaseTestCase):
        credentials = ['primary', 'admin']

    class TestPowerScaleAccessSecurityCIFS(
            _CIFSAccessSecurityTests,
            PowerScaleAccessSecurityTest,
            tempest_test.BaseTestCase):
        credentials = ['primary', 'admin']
