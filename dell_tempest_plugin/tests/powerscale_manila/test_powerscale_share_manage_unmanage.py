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
Tempest functional tests for Dell PowerScale share manage/unmanage feature.

These tests are **complementary** to the generic Manila manage/unmanage
tempest tests (manila_tempest_tests/tests/api/admin/test_share_manage*.py
and manila_tempest_tests/tests/scenario/test_share_manage_unmanage.py).
Generic tests already cover the basic manage, unmanage, lifecycle, invalid-
param, and negative flows.  This file focuses exclusively on PowerScale-
specific behaviour that is NOT covered generically:

  * Extending a managed share   (validates _get_container_path resolution)
  * Shrinking a managed share   (validates _get_container_path resolution)
  * Deleting a managed share    (validates _delete_export fallback on CIFS)
  * Combined resize sequences   (extend + shrink on the same managed share)

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


# ======================================================================
# Base mixin — helpers only, no test methods
# ======================================================================
class PowerScaleManageUnmanageTest(object):
    """Mixin providing helpers for PowerScale manage/unmanage share tests.

    All heavy lifting (share type creation, share creation, manage,
    unmanage, extend, shrink, wait-loops, cleanup) lives here so the
    protocol-specific test mixins stay short and readable.
    """

    @classmethod
    def setup_clients(cls):
        super(PowerScaleManageUnmanageTest, cls).setup_clients()
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
    def create_share(self, protocol, share_type_name, size=1, name=None):
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
        differs from the original backend directory/export name.
        """
        share_host = share['host']
        export_locations = self._get_export_locations(share['id'])
        export_path = export_locations[0]
        if isinstance(export_path, dict):
            export_path = export_path.get('path', export_path)
        LOG.info("Original export for share %s: %s", share['id'], export_path)

        share_type_id = share.get('share_type')
        # Resolve share type name from the id if needed
        share_type_name = share_type_id

        self.unmanage_share(share['id'])

        managed = self.manage_share(
            protocol=protocol,
            export_path=export_path,
            share_type_name=share_type_name,
            service_host=share_host,
        )
        self.assertEqual(managed['status'], 'available')
        return managed

    def manage_share(self, protocol, export_path, share_type_name,
                     name=None, service_host=None):
        """Manage an existing PowerScale export as a Manila share."""
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
        """Discover the Manila host string for the PowerScale backend."""
        try:
            services = self.shares_v2_client.list_services()
            for svc in services.get('services', services):
                host = svc.get('host', '')
                if 'powerscale' in host.lower():
                    LOG.info("Discovered Manila PowerScale host: %s", host)
                    return host
        except Exception as e:
            LOG.warning("Failed to discover Manila host: %s", e)
        return getattr(CONF, 'share', {}).get(
            'powerscale_host', 'manila-host@powerscale')

    # ------------------------------------------------------------------
    # Extend / Shrink helpers
    # ------------------------------------------------------------------
    def extend_share(self, share_id, new_size):
        """Extend a share and wait for it to become available."""
        LOG.info("Extending share %s to %dG", share_id, new_size)
        self.shares_v2_client.extend_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        share = self.shares_v2_client.get_share(share_id)
        return share.get('share', share)

    def shrink_share(self, share_id, new_size):
        """Shrink a share and wait for it to become available."""
        LOG.info("Shrinking share %s to %dG", share_id, new_size)
        self.shares_v2_client.shrink_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        share = self.shares_v2_client.get_share(share_id)
        return share.get('share', share)


# ======================================================================
# NFS tests — PowerScale-specific operations on managed NFS shares
# ======================================================================
class _NFSManageUnmanageTests(object):
    """NFS manage/unmanage tests unique to PowerScale.

    Generic manage/unmanage flows (basic manage, unmanage, lifecycle,
    invalid-export, negative) are covered by the upstream manila
    tempest tests and are NOT duplicated here.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSManageUnmanageTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # 1. Extend a managed NFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d4e5f6a7-4444-5555-6666-b8c9d0e1f2a3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_managed_nfs_share(self):
        """Extend an NFS share that was managed back into Manila.

        Validates the fix for bug-2141517.  After unmanage+manage the
        Manila share name is a new UUID that does NOT match the backend
        directory.  Extend calls _get_container_path which must resolve
        the real path via export_location so it can update the
        PowerScale quota (PUT quota_set).

        Flow: create(1G) -> unmanage -> manage -> extend(2G) -> verify
        PowerScale API hit: PUT /platform/1/quota/quotas/<id>
        """
        LOG.info("=== test_extend_managed_nfs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        managed = self._manage_and_return('NFS', share)

        extended = self.extend_share(managed['id'], 2)
        self.assertEqual(extended['size'], 2)
        self.assertEqual(extended['status'], 'available')
        LOG.info("Managed NFS share %s extended to 2G", managed['id'])

    # ----------------------------------------------------------------
    # 2. Shrink a managed NFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e5f6a7b8-5555-6666-7777-c9d0e1f2a3b4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_managed_nfs_share(self):
        """Shrink an NFS share that was managed back into Manila.

        Same root cause as extend — _get_container_path must resolve the
        backend path from export_location.  Shrink additionally validates
        the hard-limit check on the PowerScale quota.

        Flow: create(2G) -> unmanage -> manage -> shrink(1G) -> verify
        PowerScale API hit: PUT /platform/1/quota/quotas/<id>
        """
        LOG.info("=== test_shrink_managed_nfs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=2,
        )
        managed = self._manage_and_return('NFS', share)

        shrunk = self.shrink_share(managed['id'], 1)
        self.assertEqual(shrunk['size'], 1)
        self.assertEqual(shrunk['status'], 'available')
        LOG.info("Managed NFS share %s shrunk to 1G", managed['id'])

    # ----------------------------------------------------------------
    # 3. Delete a managed NFS share  (path resolution)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f6a7b8c9-6666-7777-8888-d0e1f2a3b4c5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_managed_nfs_share(self):
        """Delete an NFS share that was managed back into Manila.

        Deletion of a managed share requires correct resolution of
        the backend NFS export path.  The driver must find the export
        via _get_container_path (which falls back to the export_location)
        and then remove the NFS export + quota + directory on PowerScale.

        Flow: create -> unmanage -> manage -> delete -> verify gone
        PowerScale APIs hit:
          DELETE /platform/1/protocols/nfs/exports/<id>
          DELETE /platform/1/quota/quotas/<id>
        """
        LOG.info("=== test_delete_managed_nfs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
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
    # 4. Extend then shrink a managed NFS share  (combined resize)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('0a1b2c3d-aaaa-bbbb-cccc-4e5f6a7b8c9d')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_shrink_managed_nfs_share(self):
        """Extend and then shrink a managed NFS share in one test.

        Validates that _get_container_path resolves correctly across
        multiple quota-update calls on the same managed share.  The
        path must remain valid after the first resize so that the
        second resize also succeeds.

        Flow: create(1G) -> unmanage -> manage -> extend(3G)
              -> shrink(2G) -> verify
        PowerScale API hit (twice): PUT /platform/1/quota/quotas/<id>
        """
        LOG.info("=== test_extend_shrink_managed_nfs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        managed = self._manage_and_return('NFS', share)

        extended = self.extend_share(managed['id'], 3)
        self.assertEqual(extended['size'], 3)

        shrunk = self.shrink_share(managed['id'], 2)
        self.assertEqual(shrunk['size'], 2)
        self.assertEqual(shrunk['status'], 'available')
        LOG.info("Managed NFS share %s extended to 3G then shrunk to 2G",
                 managed['id'])

    # ----------------------------------------------------------------
    # 5. Extend then delete a managed NFS share  (resize + cleanup)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('1b2c3d4e-bbbb-cccc-dddd-5f6a7b8c9d0e')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_extend_delete_nfs_share(self):
        """Extend a managed NFS share and then delete it.

        Validates that after a quota update (extend) the delete path
        still resolves correctly on PowerScale.  This exercises the
        end-to-end flow a real operator would perform: import an
        existing export, grow it, then remove it.

        Flow: create(1G) -> unmanage -> manage -> extend(2G)
              -> delete -> verify gone
        """
        LOG.info("=== test_manage_extend_delete_nfs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        managed = self._manage_and_return('NFS', share)
        managed_id = managed['id']

        extended = self.extend_share(managed_id, 2)
        self.assertEqual(extended['size'], 2)

        self.shares_v2_client.delete_share(managed_id)
        self._wait_for_share_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            managed_id,
        )
        LOG.info("Managed NFS share %s extended then deleted", managed_id)


# ======================================================================
# CIFS tests — PowerScale-specific operations on managed CIFS shares
# ======================================================================
class _CIFSManageUnmanageTests(object):
    """CIFS manage/unmanage tests unique to PowerScale.

    CIFS has an additional complexity compared to NFS: the SMB share
    name on PowerScale is the *original* share name, but after
    unmanage+manage Manila assigns a *new* UUID as share['name'].
    Bug-2142554 specifically addresses the CIFS delete path where
    _delete_export must fall back to display_name / manage_share_name
    when the lookup by share['name'] returns None.
    """

    @classmethod
    def skip_checks(cls):
        super(_CIFSManageUnmanageTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # 1. Extend a managed CIFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f2a3b4c5-cccc-dddd-eeee-d6e7f8a9b0c1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_managed_cifs_share(self):
        """Extend a CIFS share that was managed back into Manila.

        Same _get_container_path resolution as NFS but the path is
        derived from the CIFS export location (\\\\<ip>\\<name>)
        which gets mapped to /ifs/<root>/<name> on the backend.

        Flow: create(1G) -> unmanage -> manage -> extend(2G) -> verify
        PowerScale API hit: PUT /platform/1/quota/quotas/<id>
        """
        LOG.info("=== test_extend_managed_cifs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        managed = self._manage_and_return('CIFS', share)

        extended = self.extend_share(managed['id'], 2)
        self.assertEqual(extended['size'], 2)
        self.assertEqual(extended['status'], 'available')
        LOG.info("Managed CIFS share %s extended to 2G", managed['id'])

    # ----------------------------------------------------------------
    # 2. Shrink a managed CIFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a3b4c5d6-dddd-eeee-ffff-e7f8a9b0c1d2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_managed_cifs_share(self):
        """Shrink a CIFS share that was managed back into Manila.

        Validates _get_container_path resolution for CIFS path derivation
        during a shrink operation.

        Flow: create(2G) -> unmanage -> manage -> shrink(1G) -> verify
        PowerScale API hit: PUT /platform/1/quota/quotas/<id>
        """
        LOG.info("=== test_shrink_managed_cifs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=2,
        )
        managed = self._manage_and_return('CIFS', share)

        shrunk = self.shrink_share(managed['id'], 1)
        self.assertEqual(shrunk['size'], 1)
        self.assertEqual(shrunk['status'], 'available')
        LOG.info("Managed CIFS share %s shrunk to 1G", managed['id'])

    # ----------------------------------------------------------------
    # 3. Delete a managed CIFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b4c5d6e7-eeee-ffff-0000-f8a9b0c1d2e3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_managed_cifs_share(self):
        """Delete a CIFS share that was managed back into Manila.

        **Primary validation for bug-2142554.**

        After manage, Manila's share['name'] is a new UUID that does NOT
        match the SMB share name on PowerScale.  Before the fix,
        _delete_cifs_share called lookup_smb_share(share['name']) which
        returned None, causing the delete to fail.  The fix falls back
        to display_name (the manage name / original export name).

        Flow: create -> unmanage -> manage -> delete -> verify gone
        PowerScale APIs hit:
          DELETE /platform/1/protocols/smb/shares/<original_name>
          DELETE /platform/1/quota/quotas/<id>
        """
        LOG.info("=== test_delete_managed_cifs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
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
        LOG.info("Managed CIFS share %s deleted (bug-2142554 validated)",
                 managed_id)

    # ----------------------------------------------------------------
    # 4. Extend then shrink a managed CIFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('2c3d4e5f-cccc-dddd-eeee-6a7b8c9d0e1f')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_shrink_managed_cifs_share(self):
        """Extend and then shrink a managed CIFS share.

        Validates that path resolution via export_location stays
        correct across multiple resize operations on a managed CIFS
        share.

        Flow: create(1G) -> unmanage -> manage -> extend(3G)
              -> shrink(2G) -> verify
        PowerScale API hit (twice): PUT /platform/1/quota/quotas/<id>
        """
        LOG.info("=== test_extend_shrink_managed_cifs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        managed = self._manage_and_return('CIFS', share)

        extended = self.extend_share(managed['id'], 3)
        self.assertEqual(extended['size'], 3)

        shrunk = self.shrink_share(managed['id'], 2)
        self.assertEqual(shrunk['size'], 2)
        self.assertEqual(shrunk['status'], 'available')
        LOG.info("Managed CIFS share %s extended to 3G then shrunk to 2G",
                 managed['id'])

    # ----------------------------------------------------------------
    # 5. Shrink then delete a managed CIFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('3d4e5f6a-dddd-eeee-ffff-7b8c9d0e1f2a')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_shrink_delete_cifs_share(self):
        """Shrink a managed CIFS share and then delete it.

        Validates both bug fixes in sequence on a CIFS share:
          1. Shrink succeeds because _get_container_path resolves the
             backend path from the CIFS export location  (bug-2141517).
          2. Delete succeeds because _delete_export falls back to
             display_name for the SMB share lookup  (bug-2142554).

        Flow: create(2G) -> unmanage -> manage -> shrink(1G)
              -> delete -> verify gone
        """
        LOG.info("=== test_manage_shrink_delete_cifs_share ===")

        share_type = self.create_manage_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=2,
        )
        managed = self._manage_and_return('CIFS', share)
        managed_id = managed['id']

        shrunk = self.shrink_share(managed_id, 1)
        self.assertEqual(shrunk['size'], 1)

        self.shares_v2_client.delete_share(managed_id)
        self._wait_for_share_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            managed_id,
        )
        LOG.info("Managed CIFS share %s shrunk then deleted "
                 "(both bugs validated)", managed_id)


# ======================================================================
# Concrete test classes wired to a Tempest-compatible base class
# ======================================================================
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleManageUnmanageNFS(
            _NFSManageUnmanageTests,
            PowerScaleManageUnmanageTest,
            manila_base.BaseSharesAdminTest):
        """NFS manage/unmanage functional tests (manila_tempest_tests base)."""

    class TestPowerScaleManageUnmanageCIFS(
            _CIFSManageUnmanageTests,
            PowerScaleManageUnmanageTest,
            manila_base.BaseSharesAdminTest):
        """CIFS manage/unmanage functional tests (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleManageUnmanageNFS(
            _NFSManageUnmanageTests,
            PowerScaleManageUnmanageTest,
            tempest_test.BaseTestCase):
        """NFS manage/unmanage functional tests (tempest.test fallback)."""
        credentials = ['primary', 'admin']

    class TestPowerScaleManageUnmanageCIFS(
            _CIFSManageUnmanageTests,
            PowerScaleManageUnmanageTest,
            tempest_test.BaseTestCase):
        """CIFS manage/unmanage functional tests (tempest.test fallback)."""
        credentials = ['primary', 'admin']
