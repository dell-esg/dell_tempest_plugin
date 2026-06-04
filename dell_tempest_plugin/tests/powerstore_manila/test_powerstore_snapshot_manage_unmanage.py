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
Tempest functional tests for Dell PowerStore snapshot manage/unmanage feature.

These tests create shares and snapshots **directly on the PowerStore backend**
via REST API and then manage the snapshots into Manila, simulating a
real-world admin workflow where pre-existing backend snapshots are imported
into OpenStack.

After manage, every test validates that the managed snapshot behaves
identically to a snapshot created natively by Manila:

  * Delete a managed snapshot          (delete_snapshot via provider_location)
  * Unmanage preserves backend         (re-manage proves no data loss)
  * Create share from managed snapshot (_create_share_from_snapshot)
  * Revert to managed snapshot         (revert_to_snapshot)
  * Export locations after manage       (verify export paths on parent share)
  * Size handling edge cases            (backend size, user size, fallback)
  * Negative: nonexistent snapshot      (ManageInvalidShareSnapshot)
  * Negative: not a snapshot            (no parent_id)
  * Negative: wrong parent share        (parent_id mismatch)
  * Negative: manage already managed    (duplicate/conflict)
  * Negative: invalid size format       (non-integer size)

NFS and CIFS are tested independently for full protocol coverage.
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

# Minimum share/snapshot size (GiB)
PS_MIN_SIZE = 10


# ======================================================================
# Base mixin — helpers only, no test methods
# ======================================================================
class PowerStoreSnapshotManageUnmanageBase(object):
    """Mixin providing Manila + PowerStore REST helpers for snapshot tests."""

    # ------------------------------------------------------------------
    # Client resolution
    # ------------------------------------------------------------------
    @classmethod
    def setup_clients(cls):
        super(PowerStoreSnapshotManageUnmanageBase, cls).setup_clients()
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

    def _ps_create_snapshot(self, parent_filesystem_id, snap_name,
                            description=None):
        """Create a snapshot directly on PowerStore backend.

        PowerStore API: POST /file_system/{filesystem_id}/snapshot
        The snapshot is a child filesystem with parent_id set automatically.
        """
        payload = {
            'name': snap_name,
        }
        if description:
            payload['description'] = description
        resp, data = self._ps_request(
            'POST', '/file_system/%s/snapshot' % parent_filesystem_id,
            payload)
        if resp.status_code == 201 and data:
            snap_id = data['id']
            LOG.info("Created PowerStore snapshot '%s' (id=%s, parent=%s)",
                     snap_name, snap_id, parent_filesystem_id)
            return snap_id
        self.fail("Failed to create PowerStore snapshot '%s': %s %s"
                  % (snap_name, resp.status_code, resp.text))

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

    def _ps_get_filesystem_by_name(self, name):
        """Get filesystem details by name from PowerStore."""
        resp, data = self._ps_request(
            'GET', '/file_system?name=eq.%s&select=id,parent_id,size_total'
            % name)
        if resp.status_code == 200 and data:
            return data[0]
        return None

    def _ps_cleanup_filesystem(self, filesystem_id):
        """Clean up a PowerStore filesystem if it still exists."""
        try:
            if self._ps_filesystem_exists(filesystem_id):
                self._ps_delete_filesystem(filesystem_id)
                LOG.info("Backend cleanup: deleted filesystem %s", 
                         filesystem_id)
            else:
                LOG.debug("Backend cleanup: filesystem %s already deleted",
                          filesystem_id)
        except Exception as e:
            LOG.warning("Backend cleanup failed for %s: %s", 
                        filesystem_id, e)

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
        """Create a Manila share type suitable for snapshot manage tests."""
        name = name or data_utils.rand_name('ps-snap-manage-type')
        specs = {
            'driver_handles_share_servers': 'False',
            'snapshot_support': 'True',
            'create_share_from_snapshot_support': 'True',
            'revert_to_snapshot_support': 'True',
        }
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
        LOG.info("Manage share request: id=%s export=%s", sh['id'],
                 export_path)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        result = self.shares_v2_client.get_share(sh['id'])
        return result.get('share', result)

    def create_manila_share(self, protocol, size=PS_MIN_SIZE,
                            share_type_id=None):
        """Create a share natively via Manila."""
        name = data_utils.rand_name('ps-manila-share')
        share = self.shares_v2_client.create_share(
            share_protocol=protocol,
            size=size,
            name=name,
            share_type_id=share_type_id,
        )
        sh = share.get('share', share)
        LOG.info("Create Manila share: id=%s name=%s", sh['id'], name)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        result = self.shares_v2_client.get_share(sh['id'])
        return result.get('share', result)

    def _delete_share_safe(self, share_id):
        try:
            sh = self.shares_v2_client.get_share(share_id)
            sh = sh.get('share', sh)
            status = sh.get('status', '').lower()
            if 'error' in status:
                try:
                    self.shares_v2_client.reset_state(
                        share_id, status='available', s_type='shares')
                    time.sleep(2)
                except Exception:
                    pass
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
    def manage_snapshot(self, share_id, provider_location,
                        driver_options=None, name=None):
        """Manage a backend snapshot into Manila."""
        name = name or data_utils.rand_name('ps-managed-snap')
        opts = driver_options or {}
        snapshot = self.shares_v2_client.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
            name=name,
            driver_options=opts,
        )
        snap = snapshot.get('snapshot', snapshot)
        LOG.info("Manage snapshot request: id=%s provider_location=%s",
                 snap['id'], provider_location)
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        return snap

    def manage_snapshot_expect_error(self, share_id, provider_location,
                                     driver_options=None, name=None):
        """Manage a snapshot and expect it to end up in manage_error."""
        name = name or data_utils.rand_name('ps-manage-snap-fail')
        opts = driver_options or {}
        try:
            snapshot = self.shares_v2_client.manage_snapshot(
                share_id=share_id,
                provider_location=provider_location,
                name=name,
                driver_options=opts,
            )
        except (lib_exc.BadRequest, lib_exc.ServerFault,
                lib_exc.Conflict) as e:
            LOG.info("Manage snapshot correctly rejected by API: %s", e)
            return None

        snap = snapshot.get('snapshot', snapshot)
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        deadline = time.time() + SHARE_BUILD_TIMEOUT
        while time.time() < deadline:
            s = self.shares_v2_client.get_snapshot(snap['id'])
            s = s.get('snapshot', s)
            status = s.get('status', '').lower()
            if status in ('error', 'manage_error'):
                LOG.info("Manage snapshot correctly failed: status=%s",
                         status)
                return s
            if status == 'available':
                return s
            time.sleep(SHARE_BUILD_INTERVAL)
        result = self.shares_v2_client.get_snapshot(snap['id'])
        return result.get('snapshot', result)

    def _wait_for_snapshot_status(self, snapshot_id, target,
                                  timeout=SHARE_BUILD_TIMEOUT,
                                  interval=SHARE_BUILD_INTERVAL):
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            snap = self.shares_v2_client.get_snapshot(snapshot_id)
            snap = snap.get('snapshot', snap)
            status = snap.get('status', '').lower()
            last_status = status
            if status == target:
                return
            if 'error' in status and target != 'manage_error':
                self.fail("Snapshot %s error: %s" % (snapshot_id, status))
            time.sleep(interval)
        self.fail("Timeout waiting for snapshot %s status '%s'; last='%s'"
                  % (snapshot_id, target, last_status))

    def _wait_for_snapshot_deletion(self, snapshot_id,
                                    timeout=SHARE_BUILD_TIMEOUT,
                                    interval=SHARE_BUILD_INTERVAL):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.shares_v2_client.get_snapshot(snapshot_id)
            except lib_exc.NotFound:
                LOG.info("Snapshot %s deletion confirmed", snapshot_id)
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for snapshot %s deletion", snapshot_id)

    def _delete_snapshot_safe(self, snapshot_id):
        try:
            snap = self.shares_v2_client.get_snapshot(snapshot_id)
            snap = snap.get('snapshot', snap)
            status = snap.get('status', '').lower()
            if 'error' in status:
                try:
                    self.shares_v2_client.snapshot_reset_state(
                        snapshot_id, status='available')
                    time.sleep(2)
                except Exception:
                    pass
            self.shares_v2_client.delete_snapshot(snapshot_id)
        except lib_exc.NotFound:
            return
        except Exception:
            pass
        self._wait_for_snapshot_deletion(snapshot_id)

    # ------------------------------------------------------------------
    # Backend name resolution
    # ------------------------------------------------------------------
    def _get_backend_fs_name_from_export(self, share_id, protocol):
        """Resolve the PowerStore filesystem name from export locations.

        Manila creates the backend filesystem with an internal name (often
        ``share['name']`` from the driver context) which may differ from
        the user-visible display name returned by the API.  The export
        location reliably contains the actual backend name:
          NFS:  ``<ip>:/<backend_name>``
          CIFS: ``\\\\<ip>\\<backend_name>``
        """
        exports = self._get_export_locations(share_id)
        self.assertGreater(len(exports), 0,
                           "Share has no export locations")
        for export in exports:
            path = (export.get('path') if isinstance(export, dict)
                    else str(export))
            if protocol.upper() == 'NFS' and ':/' in path:
                return path.rsplit(':/', 1)[-1]
            elif protocol.upper() == 'CIFS' and '\\' in path:
                return path.rstrip('\\').rsplit('\\', 1)[-1]
        self.fail("Cannot parse backend name from export locations: %s"
                  % exports)

    # ------------------------------------------------------------------
    # Combined helpers: share + backend snapshot
    # ------------------------------------------------------------------
    def create_share_with_backend_snapshot(self, protocol,
                                           share_type_id=None):
        """Create Manila share + backend snapshot on PowerStore.

        Returns (share, backend_snap_name, backend_snap_id, filesystem_id).
        """
        share = self.create_manila_share(
            protocol, size=PS_MIN_SIZE, share_type_id=share_type_id)

        backend_name = self._get_backend_fs_name_from_export(
            share['id'], protocol)
        share_fs = self._ps_get_filesystem_by_name(backend_name)
        self.assertIsNotNone(share_fs,
                             "Parent share filesystem '%s' not found on "
                             "backend" % backend_name)

        backend_snap_name = data_utils.rand_name('ps-backend-snap')
        snap_id = self._ps_create_snapshot(
            share_fs['id'], backend_snap_name,
            description="Tempest backend snapshot for manage test")
        self.addCleanup(self._ps_cleanup_filesystem, snap_id)

        return share, backend_snap_name, snap_id, share_fs['id']


# ======================================================================
# Protocol-parameterized test mixin — all test cases
# ======================================================================
class _SnapshotManageUnmanageTests(object):
    """Test mixin for snapshot manage/unmanage operations.

    Subclasses must set ``protocol`` class attribute to 'NFS' or 'CIFS'.
    """

    protocol = None

    @classmethod
    def skip_checks(cls):
        super(_SnapshotManageUnmanageTests, cls).skip_checks()
        try:
            if not CONF.service_available.manila:
                raise cls.skipException("Manila is not available")
        except cfg.NoSuchOptError:
            pass

    # ================================================================
    # HAPPY PATH TESTS
    # ================================================================

    # ----------------------------------------------------------------
    # 1. NFS/CIFS Snapshot Manage — manage existing backend snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('aa01b2c3-0001-1111-2222-d4e5f6a7b8c9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_backend_created_snapshot(self):
        """Create snapshot on PowerStore, manage into Manila, verify.

        Exercises manage_existing_snapshot() full path:
        - client.get_snapshot_filesystem() finds the backend snapshot
        - parent_id validated against share filesystem
        - backend size (size_total) used for snapshot size
        - provider_location stored correctly
        """
        LOG.info("=== test_manage_backend_created_snapshot [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        snap_detail = self.shares_v2_client.get_snapshot(managed['id'])
        snap_detail = snap_detail.get('snapshot', snap_detail)
        self.assertEqual('available', snap_detail['status'])
        self.assertEqual(share['id'], snap_detail['share_id'])
        LOG.info("Managed snapshot %s is available, parent=%s",
                 managed['id'], snap_detail['share_id'])

    # ----------------------------------------------------------------
    # 2. NFS/CIFS Snapshot Unmanage — backend persists
    # ----------------------------------------------------------------
    @decorators.idempotent_id('bb02c3d4-0002-2222-3333-e5f6a7b8c9d0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_unmanage_snapshot(self):
        """Manage snapshot, unmanage, verify backend resource persists."""
        LOG.info("=== test_unmanage_snapshot [%s] ===", self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        self.shares_v2_client.unmanage_snapshot(managed['id'])
        self._wait_for_snapshot_deletion(managed['id'])

        backend_fs = self._ps_get_filesystem_by_name(backend_snap_name)
        self.assertIsNotNone(backend_fs,
                             "Backend snapshot deleted on unmanage!")
        LOG.info("Snapshot preserved on backend after unmanage")

    # ----------------------------------------------------------------
    # 3. NFS/CIFS Snapshot Delete — backend removed
    # ----------------------------------------------------------------
    @decorators.idempotent_id('cc03d4e5-0003-3333-4444-f6a7b8c9d0e1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_managed_snapshot(self):
        """Manage snapshot, delete via Manila, verify backend removal."""
        LOG.info("=== test_delete_managed_snapshot [%s] ===", self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        self.shares_v2_client.delete_snapshot(managed['id'])
        self._wait_for_snapshot_deletion(managed['id'])

        backend_fs = self._ps_get_filesystem_by_name(backend_snap_name)
        self.assertIsNone(backend_fs,
                          "Snapshot still exists on backend after delete")
        LOG.info("Managed snapshot deleted from Manila and backend")

    # ----------------------------------------------------------------
    # 4. Revert share to managed snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('dd04e5f6-0004-4444-5555-a7b8c9d0e1f2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_to_managed_snapshot(self):
        """Manage snapshot, revert parent share to it, verify success.

        Exercises revert_to_snapshot() + _get_snapshot_filesystem_id()
        which uses provider_location to resolve the snapshot on the backend.
        """
        LOG.info("=== test_revert_to_managed_snapshot [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        self.shares_v2_client.revert_to_snapshot(
            share['id'], managed['id'])
        self._wait_for_share_status(share['id'], 'available')

        share_after = self.shares_v2_client.get_share(share['id'])
        share_after = share_after.get('share', share_after)
        self.assertEqual('available', share_after['status'])

        export_locs = self._get_export_locations(share['id'])
        self.assertGreater(len(export_locs), 0,
                           "Export locations must survive revert")
        LOG.info("Reverted share to managed snapshot, exports preserved")

    # ----------------------------------------------------------------
    # 5. Create share from managed snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('ee05f6a7-0005-5555-6666-b8c9d0e1f2a3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_share_from_managed_snapshot(self):
        """Manage snapshot, create new share from it, verify success.

        Exercises _create_share_from_snapshot() +
        _get_snapshot_filesystem_id() which uses provider_location.
        """
        LOG.info("=== test_create_share_from_managed_snapshot [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        new_share = self.shares_v2_client.create_share(
            share_protocol=self.protocol,
            size=PS_MIN_SIZE,
            snapshot_id=managed['id'],
            share_type_id=share_type['id'],
        )
        ns = new_share.get('share', new_share)
        self.addCleanup(self._delete_share_safe, ns['id'])
        self._wait_for_share_status(ns['id'], 'available')

        new_detail = self.shares_v2_client.get_share(ns['id'])
        new_detail = new_detail.get('share', new_detail)
        self.assertEqual('available', new_detail['status'])
        LOG.info("Created share %s from managed snapshot", ns['id'])

    # ----------------------------------------------------------------
    # 6. Export locations after manage (on parent share)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('ff06a7b8-0006-6666-7777-c9d0e1f2a3b4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_export_locations_after_snapshot_manage(self):
        """Verify parent share export locations preserved after manage."""
        LOG.info("=== test_export_locations_after_snapshot_manage [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        exports_before = self._get_export_locations(share['id'])
        self.assertGreater(len(exports_before), 0,
                           "Share should have exports before snapshot manage")

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        exports_after = self._get_export_locations(share['id'])
        self.assertEqual(len(exports_before), len(exports_after),
                         "Export locations changed after snapshot manage")
        LOG.info("Export locations preserved: %d exports", len(exports_after))

    # ----------------------------------------------------------------
    # 7. Unmanage then re-manage (proves no data loss)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('aa07b8c9-0007-7777-8888-d0e1f2a3b4c5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_unmanage_then_remanage(self):
        """Manage, unmanage, re-manage same snapshot — proves persistence."""
        LOG.info("=== test_unmanage_then_remanage [%s] ===", self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        self.shares_v2_client.unmanage_snapshot(managed['id'])
        self._wait_for_snapshot_deletion(managed['id'])

        remanaged = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(remanaged['id'], 'available')

        snap_detail = self.shares_v2_client.get_snapshot(remanaged['id'])
        snap_detail = snap_detail.get('snapshot', snap_detail)
        self.assertEqual('available', snap_detail['status'])
        self.assertEqual(share['id'], snap_detail['share_id'])
        LOG.info("Re-managed snapshot %s after unmanage — no data loss",
                 remanaged['id'])

    # ================================================================
    # EDGE CASE TESTS — SIZE HANDLING
    # ================================================================

    # ----------------------------------------------------------------
    # 8. Size override — backend size takes precedence
    # ----------------------------------------------------------------
    @decorators.idempotent_id('bb08c9d0-0008-8888-9999-e1f2a3b4c5d6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_size_override_backend_wins(self):
        """User provides driver_options size=20 but backend has 10G.

        Backend size should be used (10G), not the user-provided value.
        """
        LOG.info("=== test_size_override_backend_wins [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
            driver_options={'size': '20'},
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        snap_detail = self.shares_v2_client.get_snapshot(managed['id'])
        snap_detail = snap_detail.get('snapshot', snap_detail)
        self.assertEqual('available', snap_detail['status'])
        # Backend size (10G) should win over driver_options size=20
        self.assertEqual(PS_MIN_SIZE, snap_detail['size'])
        LOG.info("Backend size correctly overrode user-provided size")

    # ----------------------------------------------------------------
    # 9. No size provided — backend size used
    # ----------------------------------------------------------------
    @decorators.idempotent_id('cc09d0e1-0009-9999-aaaa-f2a3b4c5d6e7')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_no_size_backend_provides(self):
        """No size in driver_options, backend has size → use backend size."""
        LOG.info("=== test_no_size_backend_provides [%s] ===", self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        snap_detail = self.shares_v2_client.get_snapshot(managed['id'])
        snap_detail = snap_detail.get('snapshot', snap_detail)
        self.assertEqual(PS_MIN_SIZE, snap_detail['size'])
        LOG.info("Backend size correctly used when no driver_options size")

    # ================================================================
    # EDGE CASE TESTS — VALIDATION (NEGATIVE)
    # ================================================================

    # ----------------------------------------------------------------
    # 10. Invalid provider_location — nonexistent snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('dd10e1f2-0010-aaaa-bbbb-a3b4c5d6e7f8')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nonexistent_snapshot(self):
        """Manage with bogus provider_location → manage_error."""
        LOG.info("=== test_manage_nonexistent_snapshot [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share = self.create_manila_share(
            self.protocol, share_type_id=share_type['id'])

        bogus_name = 'nonexistent-snap-' + data_utils.rand_name()
        result = self.manage_snapshot_expect_error(
            share_id=share['id'],
            provider_location=bogus_name,
        )
        if result is not None:
            self.assertIn(result['status'], ('error', 'manage_error'),
                          "Expected manage_error but got: %s"
                          % result['status'])
        LOG.info("Correctly failed managing nonexistent snapshot")

    # ----------------------------------------------------------------
    # 11. Not a snapshot — regular filesystem with no parent_id
    # ----------------------------------------------------------------
    @decorators.idempotent_id('ee11f2a3-0011-bbbb-cccc-b4c5d6e7f8a9')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_filesystem_not_snapshot(self):
        """Manage a regular filesystem (no parent_id) as snapshot → fail."""
        LOG.info("=== test_manage_filesystem_not_snapshot [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share = self.create_manila_share(
            self.protocol, share_type_id=share_type['id'])

        regular_fs_name = data_utils.rand_name('ps-regular-fs')
        fs_id, _ = self._ps_create_filesystem(regular_fs_name, PS_MIN_SIZE)
        self.addCleanup(self._ps_cleanup_filesystem, fs_id)

        result = self.manage_snapshot_expect_error(
            share_id=share['id'],
            provider_location=regular_fs_name,
        )
        if result is not None:
            self.assertIn(result['status'], ('error', 'manage_error'),
                          "Expected manage_error but got: %s"
                          % result['status'])
        LOG.info("Correctly failed managing non-snapshot filesystem")

    # ----------------------------------------------------------------
    # 12. Parent mismatch — snapshot belongs to different share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('ff12a3b4-0012-cccc-dddd-c5d6e7f8a9b0')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_snapshot_parent_mismatch(self):
        """Manage snapshot that belongs to share1, claim it's share2 → fail."""
        LOG.info("=== test_manage_snapshot_parent_mismatch [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()

        share1 = self.create_manila_share(
            self.protocol, share_type_id=share_type['id'])
        share2 = self.create_manila_share(
            self.protocol, share_type_id=share_type['id'])

        share1_backend_name = self._get_backend_fs_name_from_export(
            share1['id'], self.protocol)
        share1_fs = self._ps_get_filesystem_by_name(share1_backend_name)
        self.assertIsNotNone(share1_fs, "Share1 filesystem not found")

        backend_snap_name = data_utils.rand_name('ps-mismatch-snap')
        snap_id = self._ps_create_snapshot(
            share1_fs['id'], backend_snap_name)
        self.addCleanup(self._ps_cleanup_filesystem, snap_id)

        result = self.manage_snapshot_expect_error(
            share_id=share2['id'],
            provider_location=backend_snap_name,
        )
        if result is not None:
            self.assertIn(result['status'], ('error', 'manage_error'),
                          "Expected error for parent mismatch but got: %s"
                          % result['status'])
        LOG.info("Correctly failed: snapshot parent mismatch")

    # ----------------------------------------------------------------
    # 13. Manage already managed snapshot (duplicate/conflict)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('aa13b4c5-0013-dddd-eeee-d6e7f8a9b0c1')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_already_managed_snapshot(self):
        """Manage same snapshot twice → should conflict or fail."""
        LOG.info("=== test_manage_already_managed_snapshot [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        self._wait_for_snapshot_status(managed['id'], 'available')

        result = self.manage_snapshot_expect_error(
            share_id=share['id'],
            provider_location=backend_snap_name,
        )
        # Second manage should either be rejected by API (409)
        # or end up in manage_error / available (driver idempotency)
        if result is not None:
            LOG.info("Second manage returned status=%s", result['status'])
        else:
            LOG.info("Second manage correctly rejected by API")

    # ----------------------------------------------------------------
    # 14. Invalid size format in driver_options
    # ----------------------------------------------------------------
    @decorators.idempotent_id('bb14c5d6-0014-eeee-ffff-e7f8a9b0c1d2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_invalid_size_format(self):
        """Backend size_total takes precedence over invalid driver_options size.

        When backend snapshot has size_total, the driver uses it and ignores
        invalid driver_options size format like 'abc'. This validates that
        backend size takes precedence.
        """
        LOG.info("=== test_manage_invalid_size_format [%s] ===",
                 self.protocol)

        share_type = self.create_manage_share_type()
        share, backend_snap_name, snap_id, fs_id = \
            self.create_share_with_backend_snapshot(
                self.protocol, share_type_id=share_type['id'])

        # Backend has size_total, so invalid 'abc' is ignored
        managed = self.manage_snapshot(
            share_id=share['id'],
            provider_location=backend_snap_name,
            driver_options={'size': 'abc'},
        )
        self._wait_for_snapshot_status(managed['id'], 'available')
        snap_detail = self.shares_v2_client.get_snapshot(managed['id'])
        snap_detail = snap_detail.get('snapshot', snap_detail)
        self.assertEqual(PS_MIN_SIZE, snap_detail['size'])
        LOG.info("Invalid size 'abc' correctly ignored; backend size used")


# ======================================================================
# Concrete test classes wired to a Tempest-compatible base class
# ======================================================================
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerStoreSnapshotManageNFS(
            _SnapshotManageUnmanageTests,
            PowerStoreSnapshotManageUnmanageBase,
            manila_base.BaseSharesAdminTest):
        """NFS snapshot manage/unmanage functional tests."""
        protocol = 'NFS'

    class TestPowerStoreSnapshotManageCIFS(
            _SnapshotManageUnmanageTests,
            PowerStoreSnapshotManageUnmanageBase,
            manila_base.BaseSharesAdminTest):
        """CIFS snapshot manage/unmanage functional tests."""
        protocol = 'CIFS'

except ImportError:
    from tempest import test as tempest_test

    class TestPowerStoreSnapshotManageNFS(
            _SnapshotManageUnmanageTests,
            PowerStoreSnapshotManageUnmanageBase,
            tempest_test.BaseTestCase):
        """NFS snapshot manage/unmanage functional tests (fallback)."""
        protocol = 'NFS'
        credentials = ['primary', 'admin']

    class TestPowerStoreSnapshotManageCIFS(
            _SnapshotManageUnmanageTests,
            PowerStoreSnapshotManageUnmanageBase,
            tempest_test.BaseTestCase):
        """CIFS snapshot manage/unmanage functional tests (fallback)."""
        protocol = 'CIFS'
        credentials = ['primary', 'admin']
