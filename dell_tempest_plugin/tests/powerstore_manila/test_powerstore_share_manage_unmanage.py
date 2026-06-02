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

These tests are **complementary** to the generic Manila manage/unmanage
tempest tests (manila_tempest_tests/tests/api/admin/test_share_manage*.py
and manila_tempest_tests/tests/scenario/test_share_manage_unmanage.py).
Generic tests already cover the basic manage, unmanage, lifecycle, invalid-
param, and negative flows.  This file focuses exclusively on PowerStore-
specific behaviour that is NOT covered generically:

  * Extending a managed share   (validates _get_backend_share_name resolution)
  * Deleting a managed share    (validates backend cleanup via resolved name)
  * Unmanage preserves backend  (re-manage proves no data loss)
  * Negative: nonexistent export (ManageInvalidShare error path)

PowerStore-specific context:
  - PowerStore does NOT support renaming filesystems, NFS exports, or SMB
    shares.  After manage, the backend resource keeps its original name.
  - The helper _get_backend_share_name() resolves the real backend name
    from export_locations for all subsequent operations.
  - NFS and CIFS use different path-parsing logic in the helper, so both
    protocols are tested independently.
  - Minimum share size on PowerStore is 3 GiB.
"""

import time

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

# PowerStore minimum filesystem size is 3 GiB
PS_MIN_SIZE = 3


# ======================================================================
# Base mixin — helpers only, no test methods
# ======================================================================
class PowerStoreShareManageUnmanageBase(object):
    """Mixin providing helpers for PowerStore share manage/unmanage tests."""

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
            LOG.info("Deleted share type %s", share_type_id)
        except lib_exc.NotFound:
            LOG.debug("Share type %s already gone", share_type_id)
        except Exception as e:
            LOG.warning("Failed to delete share type %s: %s",
                        share_type_id, e)

    # ------------------------------------------------------------------
    # Share helpers
    # ------------------------------------------------------------------
    def create_share(self, protocol, share_type_name, size=PS_MIN_SIZE,
                     name=None):
        """Create a Manila share and wait until it becomes available."""
        name = name or data_utils.rand_name(
            f'ps-manage-{protocol.lower()}')
        share = self.shares_v2_client.create_share(
            share_protocol=protocol,
            size=size,
            name=name,
            share_type_id=share_type_name,
        )
        sh = share.get('share', share)
        LOG.info("Created share '%s' (id=%s, protocol=%s, size=%dG)",
                 sh['name'], sh['id'], protocol, size)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self.shares_v2_client.get_share(sh['id']).get(
            'share', self.shares_v2_client.get_share(sh['id']))

    def _delete_share_safe(self, share_id):
        try:
            self.shares_v2_client.delete_share(share_id)
            LOG.info("Requested deletion of share %s", share_id)
        except lib_exc.NotFound:
            LOG.debug("Share %s already gone", share_id)
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
                    f"Share {share_id} entered error state: {status}")
            time.sleep(interval)
        self.fail(
            f"Timeout waiting for share {share_id} to reach "
            f"'{target_status}'; last status='{last_status}'")

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

    # ------------------------------------------------------------------
    # Manage / Unmanage helpers
    # ------------------------------------------------------------------
    def _manage_and_return(self, protocol, share):
        """Unmanage a share and manage it back — common setup for every test.

        Returns the newly managed share dict whose internal Manila name
        differs from the original backend filesystem/export name.
        """
        share_host = share['host']
        export_locations = self._get_export_locations(share['id'])
        export_path = export_locations[0]
        if isinstance(export_path, dict):
            export_path = export_path.get('path', export_path)
        LOG.info("Original export for share %s: %s",
                 share['id'], export_path)

        share_type_id = share.get('share_type')

        self.unmanage_share(share['id'])

        managed = self.manage_share(
            protocol=protocol,
            export_path=export_path,
            share_type_name=share_type_id,
            service_host=share_host,
        )
        self.assertEqual(managed['status'], 'available')
        return managed

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
        LOG.info("Manage request for share '%s' (id=%s, export=%s)",
                 sh['name'], sh['id'], export_path)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self.shares_v2_client.get_share(sh['id']).get(
            'share', self.shares_v2_client.get_share(sh['id']))

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
        return self.shares_v2_client.get_share(sh['id']).get(
            'share', self.shares_v2_client.get_share(sh['id']))

    def unmanage_share(self, share_id):
        """Unmanage a share (removes from Manila, keeps on backend)."""
        self.shares_v2_client.unmanage_share(share_id)
        LOG.info("Unmanaged share %s", share_id)
        self._wait_for_share_deletion(share_id)

    def _get_export_locations(self, share_id):
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    def _get_manila_host(self):
        """Discover the Manila host string for the PowerStore backend.

        Looks for 'powerstore' in the host string first; falls back to
        the first manila-share service with a host@backend format.
        """
        try:
            services = self.shares_v2_client.list_services()
            svc_list = services.get('services', services)
            # First pass: look for 'powerstore' in host name
            for svc in svc_list:
                host = svc.get('host', '')
                if 'powerstore' in host.lower():
                    LOG.info("Discovered Manila PowerStore host: %s", host)
                    return host
            # Second pass: any manila-share service with host@backend
            for svc in svc_list:
                host = svc.get('host', '')
                binary = svc.get('binary', '')
                if binary == 'manila-share' and '@' in host:
                    pool_host = host
                    if '#' not in pool_host:
                        backend = pool_host.split('@', 1)[1]
                        pool_host = f"{pool_host}#{backend}"
                    LOG.info("Discovered Manila share host: %s", pool_host)
                    return pool_host
        except Exception as e:
            LOG.warning("Failed to discover Manila host: %s", e)
        self.skipTest("Could not discover Manila share service host")

    # ------------------------------------------------------------------
    # Extend helper
    # ------------------------------------------------------------------
    def extend_share(self, share_id, new_size):
        """Extend a share and wait for it to become available."""
        LOG.info("Extending share %s to %dG", share_id, new_size)
        self.shares_v2_client.extend_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        share = self.shares_v2_client.get_share(share_id)
        return share.get('share', share)


# ======================================================================
# NFS tests — PowerStore-specific operations on managed NFS shares
# ======================================================================
class _NFSManageUnmanageTests(object):
    """NFS manage/unmanage tests unique to PowerStore.

    Generic manage/unmanage flows are covered by upstream manila tempest
    tests.  These tests validate that _get_backend_share_name() correctly
    resolves the NFS backend name (parsed from <ip>:/<name>) after manage,
    so that extend, delete, etc. work on shares whose Manila name differs
    from the backend name.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSManageUnmanageTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # 1. Extend a managed NFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('11a2b3c4-1111-2222-3333-d5e6f7a8b9c0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_managed_nfs_share(self):
        """Extend an NFS share that was managed back into Manila.

        After unmanage+manage the Manila share name is a new UUID that
        does NOT match the backend NFS export name.  Extend calls
        _resize_filesystem which uses _get_backend_share_name() to
        resolve the real backend name from export_locations.

        Flow: create(3G) -> unmanage -> manage -> extend(4G) -> verify
        """
        LOG.info("=== test_extend_managed_nfs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=PS_MIN_SIZE,
        )
        managed = self._manage_and_return('NFS', share)

        extended = self.extend_share(managed['id'], PS_MIN_SIZE + 1)
        self.assertEqual(extended['size'], PS_MIN_SIZE + 1)
        self.assertEqual(extended['status'], 'available')
        LOG.info("Managed NFS share %s extended to %dG",
                 managed['id'], PS_MIN_SIZE + 1)

    # ----------------------------------------------------------------
    # 2. Delete a managed NFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('33c4d5e6-3333-4444-5555-f7a8b9c0d1e2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_managed_nfs_share(self):
        """Delete an NFS share that was managed back into Manila.

        Deletion requires resolving the backend NFS export name via
        _get_backend_share_name() to find and delete the filesystem.

        Flow: create -> unmanage -> manage -> delete -> verify gone
        """
        LOG.info("=== test_delete_managed_nfs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=PS_MIN_SIZE,
        )
        managed = self._manage_and_return('NFS', share)
        managed_id = managed['id']

        self.shares_v2_client.delete_share(managed_id)
        self._wait_for_share_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            managed_id,
        )
        LOG.info("Managed NFS share %s deleted successfully", managed_id)

    # ----------------------------------------------------------------
    # 3. Unmanage preserves backend (re-manage proves it)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('66f7a8b9-6666-7777-8888-c0d1e2f3a4b5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_unmanage_preserves_backend_nfs(self):
        """Unmanage removes Manila metadata but preserves the backend.

        Prove this by unmanaging and re-managing the same NFS export.
        If the backend was deleted, manage would fail.

        Flow: create -> unmanage -> verify gone -> manage -> verify ok
        """
        LOG.info("=== test_unmanage_preserves_backend_nfs ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=PS_MIN_SIZE,
        )
        original_size = share['size']
        share_host = share['host']
        export_locations = self._get_export_locations(share['id'])
        export_path = export_locations[0]
        if isinstance(export_path, dict):
            export_path = export_path.get('path', export_path)

        self.unmanage_share(share['id'])

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share['id'],
        )

        managed = self.manage_share(
            protocol='NFS',
            export_path=export_path,
            share_type_name=share_type['id'],
            service_host=share_host,
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], original_size)
        LOG.info("Backend NFS share preserved after unmanage; "
                 "re-managed as %s", managed['id'])

    # ----------------------------------------------------------------
    # 4. Manage with nonexistent NFS export (negative)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('77a8b9c0-7777-8888-9999-d1e2f3a4b5c6')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nonexistent_nfs_export(self):
        """Manage with a bogus NFS export path should fail.

        The driver calls get_nfs_export_id() which returns None,
        causing ManageInvalidShare -> manage_error status.

        Flow: manage(10.0.0.1:/nonexistent) -> expect manage_error
        """
        LOG.info("=== test_manage_nonexistent_nfs_export ===")

        share_type = self.create_manage_share_type()

        result = self.manage_share_expect_error(
            protocol='NFS',
            export_path='10.0.0.1:/nonexistent-nfs-00000000',
            share_type_name=share_type['id'],
        )
        if result is not None:
            self.assertIn(
                result['status'],
                ('error', 'manage_error'),
                "Expected manage to fail but got status: %s"
                % result['status'])
        LOG.info("Manage with nonexistent NFS export correctly failed")


# ======================================================================
# CIFS tests — PowerStore-specific operations on managed CIFS shares
# ======================================================================
class _CIFSManageUnmanageTests(object):
    """CIFS manage/unmanage tests unique to PowerStore.

    CIFS uses different path parsing (\\\\<ip>\\<name>) in
    _get_backend_share_name(), so both protocols must be tested.
    """

    @classmethod
    def skip_checks(cls):
        super(_CIFSManageUnmanageTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # 1. Extend a managed CIFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('88b9c0d1-8888-9999-aaaa-e2f3a4b5c6d7')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_managed_cifs_share(self):
        """Extend a CIFS share that was managed back into Manila.

        Validates _get_backend_share_name resolves the CIFS path
        (\\\\<ip>\\<name>) correctly for the resize operation.

        Flow: create(3G) -> unmanage -> manage -> extend(4G) -> verify
        """
        LOG.info("=== test_extend_managed_cifs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=PS_MIN_SIZE,
        )
        managed = self._manage_and_return('CIFS', share)

        extended = self.extend_share(managed['id'], PS_MIN_SIZE + 1)
        self.assertEqual(extended['size'], PS_MIN_SIZE + 1)
        self.assertEqual(extended['status'], 'available')
        LOG.info("Managed CIFS share %s extended to %dG",
                 managed['id'], PS_MIN_SIZE + 1)

    # ----------------------------------------------------------------
    # 2. Delete a managed CIFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('aad1e2f3-aaaa-bbbb-cccc-a4b5c6d7e8f9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_managed_cifs_share(self):
        """Delete a CIFS share that was managed back into Manila.

        After manage, Manila's share['name'] is a new UUID that does
        NOT match the SMB share name on PowerStore.  The driver must
        resolve the backend name via _get_backend_share_name().

        Flow: create -> unmanage -> manage -> delete -> verify gone
        """
        LOG.info("=== test_delete_managed_cifs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=PS_MIN_SIZE,
        )
        managed = self._manage_and_return('CIFS', share)
        managed_id = managed['id']

        self.shares_v2_client.delete_share(managed_id)
        self._wait_for_share_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            managed_id,
        )
        LOG.info("Managed CIFS share %s deleted successfully", managed_id)

    # ----------------------------------------------------------------
    # 3. Unmanage preserves backend (re-manage proves it)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('dda4b5c6-dddd-eeee-ffff-d7e8f9a0b1c2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_unmanage_preserves_backend_cifs(self):
        """Unmanage removes Manila metadata but preserves the backend.

        Prove this by unmanaging and re-managing the same CIFS share.

        Flow: create -> unmanage -> verify gone -> manage -> verify ok
        """
        LOG.info("=== test_unmanage_preserves_backend_cifs ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=PS_MIN_SIZE,
        )
        original_size = share['size']
        share_host = share['host']
        export_locations = self._get_export_locations(share['id'])
        export_path = export_locations[0]
        if isinstance(export_path, dict):
            export_path = export_path.get('path', export_path)

        self.unmanage_share(share['id'])

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share['id'],
        )

        managed = self.manage_share(
            protocol='CIFS',
            export_path=export_path,
            share_type_name=share_type['id'],
            service_host=share_host,
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], original_size)
        LOG.info("Backend CIFS share preserved after unmanage; "
                 "re-managed as %s", managed['id'])

    # ----------------------------------------------------------------
    # 4. Manage with nonexistent CIFS share (negative)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('eeb5c6d7-eeee-ffff-0000-e8f9a0b1c2d3')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nonexistent_cifs_share(self):
        """Manage with a bogus CIFS export path should fail.

        The driver calls get_smb_share_id() which returns None,
        causing ManageInvalidShare -> manage_error status.

        Flow: manage(\\\\10.0.0.1\\nonexistent) -> expect manage_error
        """
        LOG.info("=== test_manage_nonexistent_cifs_share ===")

        share_type = self.create_manage_share_type()

        result = self.manage_share_expect_error(
            protocol='CIFS',
            export_path='\\\\10.0.0.1\\nonexistent-cifs-00000000',
            share_type_name=share_type['id'],
        )
        if result is not None:
            self.assertIn(
                result['status'],
                ('error', 'manage_error'),
                "Expected manage to fail but got status: %s"
                % result['status'])
        LOG.info("Manage with nonexistent CIFS share correctly failed")


# ======================================================================
# Concrete test classes wired to a Tempest-compatible base class
# ======================================================================
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerStoreShareManageNFS(
            _NFSManageUnmanageTests,
            PowerStoreShareManageUnmanageBase,
            manila_base.BaseSharesAdminTest):
        """NFS manage/unmanage functional tests (manila_tempest_tests base)."""

    class TestPowerStoreShareManageCIFS(
            _CIFSManageUnmanageTests,
            PowerStoreShareManageUnmanageBase,
            manila_base.BaseSharesAdminTest):
        """CIFS manage/unmanage functional tests (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerStoreShareManageNFS(
            _NFSManageUnmanageTests,
            PowerStoreShareManageUnmanageBase,
            tempest_test.BaseTestCase):
        """NFS manage/unmanage functional tests (tempest.test fallback)."""
        credentials = ['primary', 'admin']

    class TestPowerStoreShareManageCIFS(
            _CIFSManageUnmanageTests,
            PowerStoreShareManageUnmanageBase,
            tempest_test.BaseTestCase):
        """CIFS manage/unmanage functional tests (tempest.test fallback)."""
        credentials = ['primary', 'admin']
