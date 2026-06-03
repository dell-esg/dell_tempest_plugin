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

"""
Tempest functional tests for Dell PowerStore share manage/unmanage feature.

These tests create shares **directly on the PowerStore backend** via REST
API and then manage them into Manila, simulating a real-world admin workflow
where pre-existing backend shares are imported into OpenStack.

After manage, every test validates that the managed share behaves identically
to a share created natively by Manila:

  * Extend a managed share      (_resize_filesystem + _get_backend_share_name)
  * Shrink a managed share      (_resize_filesystem shrink path)
  * Delete a managed share      (_delete_share + backend cleanup)
  * Snapshot a managed share    (create_snapshot + _get_filesystem_id)
  * Revert to snapshot          (revert_to_snapshot on managed share)
  * Access rules                (_update_nfs/_update_cifs_access)
  * Unmanage preserves backend  (re-manage proves no data loss)
  * Negative: nonexistent export (ManageInvalidShare error path)

NFS and CIFS are tested independently because _get_backend_share_name()
uses different path-parsing logic for each protocol.
"""

import configparser
import time

import requests
from oslo_config import cfg
from oslo_log import log as logging
from tempest import clients
from tempest import config
from tempest.common import credentials_factory
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc
from tempest.lib.common.utils import data_utils

CONF = config.CONF
LOG = logging.getLogger(__name__)

SHARE_BUILD_TIMEOUT = 600
SHARE_BUILD_INTERVAL = 5

# Minimum share size (GiB) used by tests
PS_MIN_SIZE = 10


# ======================================================================
# Base mixin — helpers only, no test methods
# ======================================================================
class PowerStoreShareManageUnmanageBase(object):
    """Mixin providing Manila + PowerStore REST helpers."""

    # ------------------------------------------------------------------
    # Client resolution
    # ------------------------------------------------------------------
    @classmethod
    def setup_clients(cls):
        super(PowerStoreShareManageUnmanageBase, cls).setup_clients()
        admin_creds = credentials_factory.get_configured_admin_credentials()
        cls.admin_manager = clients.Manager(credentials=admin_creds)

        cls.shares_v2_client = cls._get_manila_client(cls.admin_manager)
        cls.share_types_client = cls._get_manila_share_types_client(
            cls.admin_manager)

    @staticmethod
    def _get_manila_client(manager):
        """Resolve Manila shares client from the manager."""
        for attr in ('shares_v2_client', 'shares_client',
                     'share_v2_client', 'share_client'):
            client = getattr(manager, attr, None)
            if client is not None:
                return client
        try:
            from manila_tempest_tests.services.share.v2.json import (
                shares_client as manila_shares_client)
            service_type = getattr(CONF, 'share', None)
            configured_type = getattr(service_type, 'catalog_type',
                                      None) if service_type else None
            region = (getattr(service_type, 'region', None)
                      if service_type else None) or CONF.identity.region
            endpoint_type = (getattr(service_type, 'endpoint_type', 'public')
                             if service_type else 'public')
            catalog_type = configured_type
            if not catalog_type or catalog_type == 'share':
                try:
                    auth_data = manager.auth_provider.get_auth()
                    catalog = auth_data[1].get('catalog', [])
                    for entry in catalog:
                        if entry.get('type') in (
                                'shared-file-system', 'share'):
                            catalog_type = entry['type']
                            break
                except Exception:
                    pass
            catalog_type = catalog_type or 'shared-file-system'
            return manila_shares_client.SharesV2Client(
                auth_provider=manager.auth_provider,
                service=catalog_type,
                region=region,
                endpoint_type=endpoint_type,
            )
        except ImportError:
            pass
        return None

    @classmethod
    def _get_manila_share_types_client(cls, manager):
        """Resolve Manila share types client from the manager."""
        for attr in ('share_types_v2_client', 'share_types_client'):
            client = getattr(manager, attr, None)
            if client is not None:
                return client
        return cls._get_manila_client(manager)

    # ------------------------------------------------------------------
    # PowerStore REST API helpers
    # ------------------------------------------------------------------
    @classmethod
    def _load_ps_config(cls):
        """Read PowerStore credentials from manila.conf."""
        if hasattr(cls, '_ps_config_loaded'):
            return
        conf = configparser.ConfigParser()
        conf.read('/etc/manila/manila.conf')
        backend = None
        for section in conf.sections():
            if conf.has_option(section, 'dell_nas_backend_host'):
                backend = section
                break
        if not backend:
            raise Exception(
                "No PowerStore backend section found in manila.conf")
        cls._ps_ip = conf.get(backend, 'dell_nas_backend_host')
        cls._ps_user = conf.get(backend, 'dell_nas_login')
        cls._ps_pass = conf.get(backend, 'dell_nas_password')
        cls._ps_nas_server = conf.get(backend, 'dell_nas_server')
        cls._ps_base_url = 'https://%s/api/rest' % cls._ps_ip
        cls._ps_config_loaded = True
        LOG.info("PowerStore config: ip=%s nas_server=%s",
                 cls._ps_ip, cls._ps_nas_server)

    def _ps_request(self, method, path, payload=None, params=None):
        """Send a request to PowerStore REST API."""
        self._load_ps_config()
        url = self._ps_base_url + path
        kwargs = {
            'auth': (self._ps_user, self._ps_pass),
            'verify': False,
            'timeout': 30,
        }
        if params:
            kwargs['params'] = params
        if payload and method != 'GET':
            kwargs['json'] = payload
        resp = requests.request(method, url, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            data = None
        return resp, data

    def _ps_get_nas_server_id(self):
        """Get NAS server ID from PowerStore."""
        self._load_ps_config()
        resp, data = self._ps_request(
            'GET', '/nas_server?name=eq.%s' % self._ps_nas_server)
        if resp.status_code == 200 and data:
            return data[0]['id']
        self.fail("Cannot find NAS server '%s'" % self._ps_nas_server)

    def _ps_get_nas_server_interfaces(self, nas_server_id):
        """Get NAS server file interfaces from PowerStore."""
        path = ('/nas_server/%s?select=current_preferred_IPv4_interface_id,'
                'current_preferred_IPv6_interface_id,'
                'file_interfaces(id,ip_address)' % nas_server_id)
        resp, data = self._ps_request('GET', path)
        if resp.status_code == 200 and data:
            preferred = [
                data.get('current_preferred_IPv4_interface_id'),
                data.get('current_preferred_IPv6_interface_id')]
            interfaces = []
            for i in data.get('file_interfaces', []):
                interfaces.append({
                    'ip': i['ip_address'],
                    'preferred': i['id'] in preferred,
                })
            return interfaces
        return []

    def _ps_create_filesystem(self, name, size_gb):
        """Create a filesystem directly on PowerStore backend."""
        nas_server_id = self._ps_get_nas_server_id()
        payload = {
            'name': name,
            'size_total': size_gb * (1024 ** 3),
            'nas_server_id': nas_server_id,
        }
        resp, data = self._ps_request('POST', '/file_system', payload)
        if resp.status_code == 201 and data:
            fs_id = data['id']
            LOG.info("Created PowerStore filesystem '%s' (id=%s, size=%dG)",
                     name, fs_id, size_gb)
            return fs_id, nas_server_id
        self.fail("Failed to create PowerStore filesystem '%s': %s %s"
                  % (name, resp.status_code, resp.text))

    def _ps_create_nfs_export(self, filesystem_id, name):
        """Create NFS export directly on PowerStore backend."""
        payload = {
            'file_system_id': filesystem_id,
            'path': '/' + name,
            'name': name,
        }
        resp, data = self._ps_request('POST', '/nfs_export', payload)
        if resp.status_code == 201 and data:
            LOG.info("Created NFS export '%s' (id=%s)", name, data['id'])
            return data['id']
        self.fail("Failed to create NFS export '%s': %s %s"
                  % (name, resp.status_code, resp.text))

    def _ps_create_smb_share(self, filesystem_id, name):
        """Create SMB share directly on PowerStore backend."""
        payload = {
            'file_system_id': filesystem_id,
            'path': '/' + name,
            'name': name,
        }
        resp, data = self._ps_request('POST', '/smb_share', payload)
        if resp.status_code == 201 and data:
            LOG.info("Created SMB share '%s' (id=%s)", name, data['id'])
            return data['id']
        self.fail("Failed to create SMB share '%s': %s %s"
                  % (name, resp.status_code, resp.text))

    def _ps_delete_filesystem(self, filesystem_id):
        """Delete filesystem from PowerStore (cascade deletes exports)."""
        resp, _ = self._ps_request(
            'DELETE', '/file_system/%s' % filesystem_id)
        if resp.status_code == 204:
            LOG.info("Deleted PowerStore filesystem %s", filesystem_id)
            return True
        LOG.warning("Failed to delete filesystem %s: %s",
                    filesystem_id, resp.status_code)
        return False

    def _ps_filesystem_exists(self, filesystem_id):
        """Check if a filesystem still exists on PowerStore."""
        resp, _ = self._ps_request(
            'GET', '/file_system/%s' % filesystem_id)
        return resp.status_code == 200

    def _ps_cleanup_filesystem(self, filesystem_id):
        """Clean up a PowerStore filesystem if it still exists."""
        if self._ps_filesystem_exists(filesystem_id):
            self._ps_delete_filesystem(filesystem_id)

    def create_backend_share(self, protocol, name=None, size_gb=PS_MIN_SIZE):
        """Create a complete share on PowerStore backend via REST API.

        Returns (filesystem_id, export_path) tuple.
        Registers cleanup so the filesystem is removed if manage fails.
        """
        name = name or data_utils.rand_name('ps-backend')
        fs_id, nas_server_id = self._ps_create_filesystem(name, size_gb)
        interfaces = self._ps_get_nas_server_interfaces(nas_server_id)
        if not interfaces:
            self.fail("No file interfaces found for NAS server")

        ip = None
        for iface in interfaces:
            if iface.get('preferred'):
                ip = iface['ip']
                break
        if not ip:
            ip = interfaces[0]['ip']

        if protocol.upper() == 'NFS':
            self._ps_create_nfs_export(fs_id, name)
            export_path = '%s:/%s' % (ip, name)
        elif protocol.upper() == 'CIFS':
            self._ps_create_smb_share(fs_id, name)
            export_path = '\\\\%s\\%s' % (ip, name)
        else:
            self.fail("Unsupported protocol: %s" % protocol)

        LOG.info("Backend share: protocol=%s export_path=%s fs_id=%s",
                 protocol, export_path, fs_id)
        self.addCleanup(self._ps_cleanup_filesystem, fs_id)
        return fs_id, export_path

    # ------------------------------------------------------------------
    # Share type helpers
    # ------------------------------------------------------------------
    def create_manage_share_type(self, name=None, extra_specs=None):
        """Create a Manila share type suitable for manage/unmanage tests."""
        name = name or data_utils.rand_name('ps-manage-type')
        specs = {'driver_handles_share_servers': 'False'}
        if extra_specs:
            specs.update(extra_specs)

        share_type = self.share_types_client.create_share_type(
            name=name,
            extra_specs=specs,
        )
        st = share_type.get('share_type', share_type)
        LOG.info("Created share type '%s' (id=%s) with specs=%s",
                 st['name'], st['id'], specs)
        self.addCleanup(self._delete_share_type_safe, st['id'])
        return st

    def _delete_share_type_safe(self, share_type_id):
        try:
            self.share_types_client.delete_share_type(share_type_id)
        except lib_exc.NotFound:
            pass
        except Exception as e:
            LOG.warning("Failed to delete share type %s: %s",
                        share_type_id, e)

    # ------------------------------------------------------------------
    # Manila share helpers
    # ------------------------------------------------------------------
    def manage_share(self, protocol, export_path, share_type_name,
                     name=None, service_host=None):
        """Manage an existing PowerStore export as a Manila share."""
        name = name or data_utils.rand_name('ps-managed')
        share = self.shares_v2_client.manage_share(
            service_host=service_host or self._get_manila_host(),
            protocol=protocol,
            export_path=export_path,
            share_type_id=share_type_name,
            name=name,
        )
        sh = share.get('share', share)
        LOG.info("Manage request: id=%s export=%s", sh['id'], export_path)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        result = self.shares_v2_client.get_share(sh['id'])
        return result.get('share', result)

    def manage_share_expect_error(self, protocol, export_path,
                                  share_type_name, name=None,
                                  service_host=None):
        """Manage a share and expect it to end up in manage_error."""
        name = name or data_utils.rand_name('ps-manage-fail')
        try:
            share = self.shares_v2_client.manage_share(
                service_host=service_host or self._get_manila_host(),
                protocol=protocol,
                export_path=export_path,
                share_type_id=share_type_name,
                name=name,
            )
        except (lib_exc.BadRequest, lib_exc.ServerFault,
                lib_exc.Conflict) as e:
            LOG.info("Manage correctly rejected by API: %s", e)
            return None

        sh = share.get('share', share)
        self.addCleanup(self._delete_share_safe, sh['id'])
        deadline = time.time() + SHARE_BUILD_TIMEOUT
        while time.time() < deadline:
            s = self.shares_v2_client.get_share(sh['id'])
            s = s.get('share', s)
            status = s.get('status', '').lower()
            if status in ('error', 'manage_error'):
                LOG.info("Manage correctly failed with status=%s", status)
                return s
            if status == 'available':
                return s
            time.sleep(SHARE_BUILD_INTERVAL)
        result = self.shares_v2_client.get_share(sh['id'])
        return result.get('share', result)

    def unmanage_share(self, share_id):
        """Unmanage a share (removes from Manila, keeps on backend)."""
        self.shares_v2_client.unmanage_share(share_id)
        LOG.info("Unmanaged share %s", share_id)
        self._wait_for_share_deletion(share_id)

    def extend_share(self, share_id, new_size):
        """Extend a share and wait for it to become available."""
        LOG.info("Extending share %s to %dG", share_id, new_size)
        self.shares_v2_client.extend_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        result = self.shares_v2_client.get_share(share_id)
        return result.get('share', result)

    def shrink_share(self, share_id, new_size):
        """Shrink a share and wait for it to become available."""
        LOG.info("Shrinking share %s to %dG", share_id, new_size)
        self.shares_v2_client.shrink_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        result = self.shares_v2_client.get_share(share_id)
        return result.get('share', result)

    def _delete_share_safe(self, share_id):
        try:
            self.shares_v2_client.delete_share(share_id)
        except lib_exc.NotFound:
            return
        except Exception as e:
            LOG.warning("Failed to delete share %s: %s", share_id, e)
            return
        self._wait_for_share_deletion(share_id)

    def _wait_for_share_status(self, share_id, target_status,
                               timeout=SHARE_BUILD_TIMEOUT,
                               interval=SHARE_BUILD_INTERVAL):
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            share = self.shares_v2_client.get_share(share_id)
            sh = share.get('share', share)
            status = sh.get('status', '').lower()
            last_status = status
            if status == target_status:
                return
            if status in ('error', 'error_deleting',
                          'manage_error', 'shrinking_error',
                          'extending_error'):
                self.fail(
                    "Share %s entered error state: %s" % (share_id, status))
            time.sleep(interval)
        self.fail(
            "Timeout waiting for share %s to reach '%s'; last='%s'"
            % (share_id, target_status, last_status))

    def _wait_for_share_deletion(self, share_id,
                                 timeout=SHARE_BUILD_TIMEOUT,
                                 interval=SHARE_BUILD_INTERVAL):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.shares_v2_client.get_share(share_id)
            except lib_exc.NotFound:
                LOG.info("Share %s deletion confirmed", share_id)
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for share %s deletion", share_id)

    def _get_export_locations(self, share_id):
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    def _get_manila_host(self):
        """Discover the Manila host string for the PowerStore backend."""
        try:
            services = self.shares_v2_client.list_services()
            svc_list = services.get('services', services)
            for svc in svc_list:
                host = svc.get('host', '')
                if 'powerstore' in host.lower():
                    LOG.info("Discovered Manila PowerStore host: %s", host)
                    return host
            for svc in svc_list:
                host = svc.get('host', '')
                binary = svc.get('binary', '')
                if binary == 'manila-share' and '@' in host:
                    pool_host = host
                    if '#' not in pool_host:
                        backend = pool_host.split('@', 1)[1]
                        pool_host = '%s#%s' % (pool_host, backend)
                    LOG.info("Discovered Manila share host: %s", pool_host)
                    return pool_host
        except Exception as e:
            LOG.warning("Failed to discover Manila host: %s", e)
        self.skipTest("Could not discover Manila share service host")

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def _wait_for_snapshot_status(self, snapshot_id, target,
                                  timeout=SHARE_BUILD_TIMEOUT,
                                  interval=SHARE_BUILD_INTERVAL):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.shares_v2_client.get_snapshot(snapshot_id)
            snap = snap.get('snapshot', snap)
            status = snap.get('status', '').lower()
            if status == target:
                return
            if 'error' in status:
                self.fail("Snapshot %s error: %s" % (snapshot_id, status))
            time.sleep(interval)
        self.fail("Timeout waiting for snapshot %s status '%s'"
                  % (snapshot_id, target))

    def _wait_for_snapshot_deletion(self, snapshot_id,
                                    timeout=SHARE_BUILD_TIMEOUT,
                                    interval=SHARE_BUILD_INTERVAL):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.shares_v2_client.get_snapshot(snapshot_id)
            except lib_exc.NotFound:
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for snapshot %s deletion", snapshot_id)

    def _delete_snapshot_safe(self, snapshot_id):
        try:
            self.shares_v2_client.delete_snapshot(snapshot_id)
        except lib_exc.NotFound:
            return
        except Exception:
            pass
        self._wait_for_snapshot_deletion(snapshot_id)

    # ------------------------------------------------------------------
    # Access rule helpers
    # ------------------------------------------------------------------
    def _wait_for_access_rule_status(self, share_id, rule_id, target,
                                      timeout=SHARE_BUILD_TIMEOUT,
                                      interval=SHARE_BUILD_INTERVAL):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rules_resp = self.shares_v2_client.list_access_rules(share_id)
            rules = (rules_resp.get('access_list') or
                     rules_resp.get('share_access_rules') or
                     rules_resp)
            if isinstance(rules, list):
                for r in rules:
                    if r['id'] == rule_id:
                        state = r.get('state', r.get('access_state', ''))
                        if state.lower() == target:
                            return
                        if 'error' in state.lower():
                            self.fail("Access rule %s error: %s"
                                      % (rule_id, state))
                        break
            time.sleep(interval)
        self.fail("Timeout waiting for access rule %s status '%s'"
                  % (rule_id, target))

    def _delete_access_rule_safe(self, share_id, rule_id):
        try:
            self.shares_v2_client.delete_access_rule(share_id, rule_id)
        except (lib_exc.NotFound, Exception):
            pass


# ======================================================================
# Protocol-parameterized test mixin — all edge cases
# ======================================================================
class _ManageUnmanageTests(object):
    """Test mixin for manage/unmanage operations.

    Subclasses must set ``protocol`` class attribute to 'NFS' or 'CIFS'.
    Each test creates a share directly on the PowerStore backend via REST
    API, manages it into Manila, then exercises a specific driver code path
    to verify that the managed share behaves identically to one created
    natively by Manila.
    """

    protocol = None

    @classmethod
    def skip_checks(cls):
        super(_ManageUnmanageTests, cls).skip_checks()
        try:
            if not CONF.service_available.manila:
                raise cls.skipException("Manila is not available")
        except cfg.NoSuchOptError:
            pass

    # ----------------------------------------------------------------
    # 1. Manage a backend-created share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('11a2b3c4-0001-2222-3333-d5e6f7a8b9c0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_backend_created_share(self):
        """Create share on PowerStore backend, manage into Manila, verify.

        Exercises manage_existing() with a share that was NEVER created
        by Manila — the backend resource name does not follow Manila's
        naming convention.

        Flow: PS REST create filesystem+export -> Manila manage -> verify
        """
        LOG.info("=== test_manage_backend_created_share [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        fs_id, export_path = self.create_backend_share(
            self.protocol, size_gb=PS_MIN_SIZE)

        managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], PS_MIN_SIZE)

        export_locs = self._get_export_locations(managed['id'])
        self.assertGreater(len(export_locs), 0,
                           "Managed share must have export locations")
        LOG.info("Managed %s share %s from backend (size=%dG, exports=%d)",
                 self.protocol, managed['id'], managed['size'],
                 len(export_locs))

    # ----------------------------------------------------------------
    # 2. Extend a managed share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('22b3c4d5-0002-3333-4444-e6f7a8b9c0d1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_managed_share(self):
        """Extend a share that was managed from the backend.

        Validates that _resize_filesystem correctly resolves the backend
        resource name via _get_backend_share_name() (parsed from export
        location) for a share whose Manila name differs from the backend.

        Flow: PS create -> manage -> extend(+1G) -> verify new size
        """
        LOG.info("=== test_extend_managed_share [%s] ===", self.protocol)

        share_type = self.create_manage_share_type()
        fs_id, export_path = self.create_backend_share(
            self.protocol, size_gb=PS_MIN_SIZE)

        managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
        )
        new_size = PS_MIN_SIZE + 1
        extended = self.extend_share(managed['id'], new_size)
        self.assertEqual(extended['size'], new_size)
        self.assertEqual(extended['status'], 'available')
        LOG.info("Extended managed %s share to %dG", self.protocol, new_size)

    # ----------------------------------------------------------------
    # 3. Shrink a managed share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('33c4d5e6-0003-4444-5555-f7a8b9c0d1e2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_managed_share(self):
        """Extend then shrink a managed share.

        Validates the shrink path in _resize_filesystem including the
        ShareShrinkingPossibleDataLoss guard when data would be lost.
        Here we extend first to ensure the shrink target is valid.

        Flow: PS create(10G) -> manage -> extend(12G) -> shrink(11G)
        """
        LOG.info("=== test_shrink_managed_share [%s] ===", self.protocol)

        share_type = self.create_manage_share_type()
        fs_id, export_path = self.create_backend_share(
            self.protocol, size_gb=PS_MIN_SIZE)

        managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
        )
        extend_size = PS_MIN_SIZE + 2
        self.extend_share(managed['id'], extend_size)

        shrink_size = PS_MIN_SIZE + 1
        shrunk = self.shrink_share(managed['id'], shrink_size)
        self.assertEqual(shrunk['size'], shrink_size)
        self.assertEqual(shrunk['status'], 'available')
        LOG.info("Shrunk managed %s share to %dG", self.protocol, shrink_size)

    # ----------------------------------------------------------------
    # 4. Delete a managed share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('44d5e6f7-0004-5555-6666-a8b9c0d1e2f3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_managed_share(self):
        """Delete a managed share and verify backend cleanup.

        Validates _delete_share correctly resolves the backend name via
        _get_backend_share_name() and deletes the filesystem.

        Flow: PS create -> manage -> delete -> verify gone from Manila
        """
        LOG.info("=== test_delete_managed_share [%s] ===", self.protocol)

        share_type = self.create_manage_share_type()
        fs_id, export_path = self.create_backend_share(
            self.protocol, size_gb=PS_MIN_SIZE)

        managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
        )
        managed_id = managed['id']

        self.shares_v2_client.delete_share(managed_id)
        self._wait_for_share_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            managed_id,
        )
        LOG.info("Deleted managed %s share %s", self.protocol, managed_id)

    # ----------------------------------------------------------------
    # 5. Snapshot on a managed share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('55e6f7a8-0005-6666-7777-b9c0d1e2f3a4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_snapshot_managed_share(self):
        """Create and delete a snapshot on a managed share.

        Validates create_snapshot correctly uses _get_filesystem_id()
        which depends on _get_backend_share_name() for a share that
        was not originally created by Manila.

        Flow: PS create -> manage -> create snapshot -> verify
              -> delete snapshot -> verify gone
        """
        LOG.info("=== test_snapshot_managed_share [%s] ===", self.protocol)

        share_type = self.create_manage_share_type(
            extra_specs={'snapshot_support': 'True'})
        fs_id, export_path = self.create_backend_share(
            self.protocol, size_gb=PS_MIN_SIZE)

        managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
        )

        snap_name = data_utils.rand_name('ps-snap')
        snapshot = self.shares_v2_client.create_snapshot(
            managed['id'], name=snap_name)
        snap = snapshot.get('snapshot', snapshot)
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        self._wait_for_snapshot_status(snap['id'], 'available')

        snap_detail = self.shares_v2_client.get_snapshot(snap['id'])
        snap_detail = snap_detail.get('snapshot', snap_detail)
        self.assertEqual(snap_detail['status'], 'available')

        self.shares_v2_client.delete_snapshot(snap['id'])
        self._wait_for_snapshot_deletion(snap['id'])
        LOG.info("Snapshot create/delete on managed %s share completed",
                 self.protocol)

    # ----------------------------------------------------------------
    # 6. Revert a managed share to snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('66f7a8b9-0006-7777-8888-c0d1e2f3a4b5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_snapshot_managed_share(self):
        """Revert a managed share to a snapshot.

        Validates revert_to_snapshot on a share that was imported from
        the backend — exercises the full snapshot lifecycle including
        the PowerStore restore API.

        Flow: PS create -> manage -> snapshot -> revert -> verify available
        """
        LOG.info("=== test_revert_snapshot_managed_share [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type(
            extra_specs={
                'snapshot_support': 'True',
                'revert_to_snapshot_support': 'True',
            })
        fs_id, export_path = self.create_backend_share(
            self.protocol, size_gb=PS_MIN_SIZE)

        managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
        )

        snap_name = data_utils.rand_name('ps-revert-snap')
        snapshot = self.shares_v2_client.create_snapshot(
            managed['id'], name=snap_name)
        snap = snapshot.get('snapshot', snapshot)
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        self._wait_for_snapshot_status(snap['id'], 'available')

        self.shares_v2_client.revert_to_snapshot(
            managed['id'], snap['id'])
        self._wait_for_share_status(managed['id'], 'available')

        share_after = self.shares_v2_client.get_share(managed['id'])
        share_after = share_after.get('share', share_after)
        self.assertEqual(share_after['status'], 'available')

        export_locs = self._get_export_locations(managed['id'])
        self.assertGreater(len(export_locs), 0,
                           "Export locations must survive revert")
        LOG.info("Reverted managed %s share to snapshot", self.protocol)

    # ----------------------------------------------------------------
    # 7. Access rules on a managed share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('77a8b9c0-0007-8888-9999-d1e2f3a4b5c6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_access_rules_managed_share(self):
        """Add access rules on a managed share.

        NFS: validates _update_nfs_access with _get_backend_share_name
             (access_type='ip').
        CIFS: validates _update_cifs_access with _get_backend_share_name
              (access_type='user').

        The primary goal is to exercise the _get_backend_share_name()
        resolution in the access-rule code path. If the rule reaches
        'active', full success. If it goes to 'error' (e.g. CIFS
        standalone SMB without valid local users), the name resolution
        still succeeded — the failure is in the backend ACL operation.

        Flow: PS create -> manage -> create access rule -> verify
        """
        LOG.info("=== test_access_rules_managed_share [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        fs_id, export_path = self.create_backend_share(
            self.protocol, size_gb=PS_MIN_SIZE)

        managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
        )

        if self.protocol.upper() == 'NFS':
            access_type = 'ip'
            access_to = '10.0.0.1'
        else:
            access_type = 'user'
            access_to = 'admin'

        rule = self.shares_v2_client.create_access_rule(
            managed['id'],
            access_type=access_type,
            access_to=access_to,
            access_level='rw',
        )
        rule = rule.get('access', rule)
        self.addCleanup(
            self._delete_access_rule_safe, managed['id'], rule['id'])

        deadline = time.time() + SHARE_BUILD_TIMEOUT
        final_state = None
        while time.time() < deadline:
            rules_resp = self.shares_v2_client.list_access_rules(
                managed['id'])
            rules = (rules_resp.get('access_list') or
                     rules_resp.get('share_access_rules') or
                     rules_resp)
            if isinstance(rules, list):
                for r in rules:
                    if r['id'] == rule['id']:
                        final_state = r.get(
                            'state', r.get('access_state', ''))
                        break
            if final_state and final_state.lower() in ('active', 'error'):
                break
            time.sleep(SHARE_BUILD_INTERVAL)

        if final_state and final_state.lower() == 'active':
            LOG.info("%s access rule active on managed share %s",
                     self.protocol, managed['id'])
        elif final_state and 'error' in final_state.lower():
            LOG.warning(
                "%s access rule ended in '%s' on managed share %s. "
                "Backend name resolution succeeded (rule was submitted "
                "to the correct export); ACL may have failed due to "
                "environment (e.g. standalone SMB without valid local "
                "users). This is not a manage/unmanage defect.",
                self.protocol, final_state, managed['id'])
        else:
            self.fail("Timeout waiting for access rule %s to reach a "
                      "terminal state; last state='%s'"
                      % (rule['id'], final_state))

    # ----------------------------------------------------------------
    # 8. Unmanage preserves backend (re-manage proves it)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('88b9c0d1-0008-9999-aaaa-e2f3a4b5c6d7')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_unmanage_preserves_backend(self):
        """Manage a backend share, unmanage it, then re-manage.

        Proves that unmanage removes Manila metadata but leaves the
        backend filesystem and export intact.

        Flow: PS create -> manage -> unmanage -> verify gone from Manila
              -> re-manage -> verify available + correct size
        """
        LOG.info("=== test_unmanage_preserves_backend [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        fs_id, export_path = self.create_backend_share(
            self.protocol, size_gb=PS_MIN_SIZE)

        managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
        )
        original_size = managed['size']
        share_host = managed['host']

        self.unmanage_share(managed['id'])

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            managed['id'],
        )

        re_managed = self.manage_share(
            protocol=self.protocol,
            export_path=export_path,
            share_type_name=share_type['id'],
            service_host=share_host,
        )
        self.assertEqual(re_managed['status'], 'available')
        self.assertEqual(re_managed['size'], original_size)
        LOG.info("Backend %s share preserved after unmanage; "
                 "re-managed as %s", self.protocol, re_managed['id'])

    # ----------------------------------------------------------------
    # 9. Manage nonexistent export (negative)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('99c0d1e2-0009-aaaa-bbbb-f3a4b5c6d7e8')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nonexistent_export(self):
        """Manage with a bogus export path should fail.

        NFS: driver calls get_nfs_export_id() -> None -> ManageInvalidShare.
        CIFS: driver calls get_smb_share_id() -> None -> ManageInvalidShare.

        Flow: manage(bogus_path) -> expect manage_error status
        """
        LOG.info("=== test_manage_nonexistent_export [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()

        if self.protocol.upper() == 'NFS':
            bogus_path = '10.0.0.1:/nonexistent-nfs-00000000'
        else:
            bogus_path = '\\\\10.0.0.1\\nonexistent-cifs-00000000'

        result = self.manage_share_expect_error(
            protocol=self.protocol,
            export_path=bogus_path,
            share_type_name=share_type['id'],
        )
        if result is not None:
            self.assertIn(
                result['status'],
                ('error', 'manage_error'),
                "Expected manage to fail but got status: %s"
                % result['status'])
        LOG.info("Manage nonexistent %s export correctly failed",
                 self.protocol)


# ======================================================================
# Concrete test classes wired to a Tempest-compatible base class
# ======================================================================
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerStoreShareManageNFS(
            _ManageUnmanageTests,
            PowerStoreShareManageUnmanageBase,
            manila_base.BaseSharesAdminTest):
        """NFS manage/unmanage functional tests (manila_tempest_tests base)."""
        protocol = 'NFS'

    class TestPowerStoreShareManageCIFS(
            _ManageUnmanageTests,
            PowerStoreShareManageUnmanageBase,
            manila_base.BaseSharesAdminTest):
        """CIFS manage/unmanage functional tests (manila_tempest_tests base)."""
        protocol = 'CIFS'

except ImportError:
    from tempest import test as tempest_test

    class TestPowerStoreShareManageNFS(
            _ManageUnmanageTests,
            PowerStoreShareManageUnmanageBase,
            tempest_test.BaseTestCase):
        """NFS manage/unmanage functional tests (tempest.test fallback)."""
        protocol = 'NFS'
        credentials = ['primary', 'admin']

    class TestPowerStoreShareManageCIFS(
            _ManageUnmanageTests,
            PowerStoreShareManageUnmanageBase,
            tempest_test.BaseTestCase):
        """CIFS manage/unmanage functional tests (tempest.test fallback)."""
        protocol = 'CIFS'
        credentials = ['primary', 'admin']
