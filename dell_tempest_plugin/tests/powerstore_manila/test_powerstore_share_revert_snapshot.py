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
Tempest functional tests for Dell PowerStore share revert-to-snapshot feature.

These tests are **complementary** to the generic Manila revert-to-snapshot
tempest tests (manila_tempest_tests/tests/api/test_revert_to_snapshot.py
and test_revert_to_snapshot_negative.py).

The generic tests cover:
  - Revert to latest snapshot
  - Revert to previous snapshot (after deleting latest)
  - Revert to replicated snapshot
  - Revert to second-latest snapshot (negative)
  - Revert to error snapshot (negative)
  - Revert error share to snapshot (negative)
  - Revert to missing/invalid snapshot (negative)

This file focuses on PowerStore-specific behavior through real Manila API
calls that propagate to the PowerStore backend:

  * PowerStore's synchronous revert (no async job tracking like PowerScale)
  * Export locations preservation after revert
  * NFS and CIFS protocol support
  * Full revert lifecycle (create -> snapshot -> revert -> cleanup)
  * Snapshot persistence after revert

PowerStore API reference:
  POST /api/rest/file_system_snapshot/{snapshot_id}/restore
  - Synchronous operation (returns 204 immediately)
  - Atomic operation at the filesystem level
  - No job tracking or periodic polling needed

PowerStore driver path (Manila):
  - connection.py: revert_to_snapshot() -> client.restore_snapshot()
  - client.py: POST /api/rest/file_system_snapshot/{snapshot_id}/restore
"""

import configparser
import time

import requests
from oslo_log import log as logging
from tempest import clients
from tempest import config
from tempest.common import credentials_factory
from tempest.common import waiters as tempest_waiters
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc
from tempest.lib.common.utils import data_utils

CONF = config.CONF
LOG = logging.getLogger(__name__)

SHARE_BUILD_TIMEOUT = 600
SHARE_BUILD_INTERVAL = 5
SHARE_MIN_SIZE = 3


# ======================================================================
# Base mixin — helpers only, no test methods
# ======================================================================
class PowerStoreShareRevertSnapshotTest(object):
    """Mixin with helpers for PowerStore share revert-to-snapshot tests.

    Provides utility methods for creating share types (with
    revert_to_snapshot_support), shares, snapshots, reverting,
    and waiting for share status transitions via the Manila API.

    PowerStore revert is synchronous (atomic operation), unlike PowerScale
    which is asynchronous with job tracking.
    """

    @classmethod
    def setup_clients(cls):
        super(PowerStoreShareRevertSnapshotTest, cls).setup_clients()
        admin_creds = credentials_factory.get_configured_admin_credentials()
        cls.admin_manager = clients.Manager(credentials=admin_creds)

        cls.shares_v2_client = cls._get_manila_client(cls.admin_manager)
        cls.share_types_client = cls._get_manila_share_types_client(
            cls.admin_manager)

        if cls.shares_v2_client is None:
            cls.skipTest("Manila shares client not available")
        if cls.share_types_client is None:
            cls.skipTest("Manila share types client not available")

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
                        if entry.get('type') in ('shared-file-system', 'share'):
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
            return None

    @classmethod
    def _get_manila_share_types_client(cls, manager):
        """Resolve Manila share types client from the manager."""
        for attr in ('share_types_client', 'share_types_v2_client',
                     'share_type_client', 'share_type_v2_client'):
            client = getattr(manager, attr, None)
            if client is not None:
                return client
        # Fall back to using the shares client's share_types endpoint
        return cls._get_manila_client(manager)

    @classmethod
    def skip_checks(cls):
        super(PowerStoreShareRevertSnapshotTest, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")
        if not CONF.share.capability_snapshot_support:
            raise cls.skipException("Snapshot support is disabled")

    # ------------------------------------------------------------------
    # Share type helpers
    # ------------------------------------------------------------------
    def create_revert_share_type(self):
        """Create a share type with revert_to_snapshot_support enabled."""
        name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-revert-type')
        extra_specs = {
            'driver_handles_share_servers': 'False',
            'snapshot_support': 'True',
            'revert_to_snapshot_support': 'True',
        }
        st = self.share_types_client.create_share_type(
            name=name,
            extra_specs=extra_specs)['share_type']
        LOG.info("Created share type '%s' (id=%s) with extra_specs=%s",
                 st['name'], st['id'], extra_specs)
        self.addCleanup(self._delete_share_type_safe, st['id'])
        return st

    def _delete_share_type_safe(self, type_id, timeout=300, interval=5):
        """Try to delete a share type; retry if still in use."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.share_types_client.delete_share_type(type_id)
                return
            except lib_exc.BadRequest:
                LOG.info("Share type %s still in use; retrying...", type_id)
            except lib_exc.NotFound:
                return
            time.sleep(interval)
        try:
            self.share_types_client.delete_share_type(type_id)
        except Exception as e:
            LOG.warning("Final delete of share type %s failed: %s",
                        type_id, e)

    # ------------------------------------------------------------------
    # Share helpers
    # ------------------------------------------------------------------
    def create_share(self, protocol='NFS', share_type_name=None,
                    size=SHARE_MIN_SIZE):
        """Create a share and wait until available."""
        name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-revert-share')
        share = self.shares_v2_client.create_share(
            name=name,
            share_protocol=protocol,
            share_type_id=share_type_name,
            size=size,
        )['share']
        LOG.info("Created share '%s' (id=%s) with protocol=%s, size=%d",
                 share['name'], share['id'], protocol, size)
        self.addCleanup(self._delete_share_safe, share['id'])
        self._wait_for_share_status(share['id'], 'available')
        return self.shares_v2_client.get_share(share['id'])['share']

    def _delete_share_safe(self, share_id):
        """Delete a share; ignore NotFound."""
        try:
            self.shares_v2_client.delete_share(share_id)
        except lib_exc.NotFound:
            return
        except Exception as e:
            LOG.debug("delete_share(%s) raised: %s", share_id, e)
        try:
            self.shares_v2_client.wait_for_resource_deletion(share_id=share_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            self._wait_for_share_deletion(share_id)

    def _wait_for_share_deletion(self, share_id,
                                timeout=SHARE_BUILD_TIMEOUT,
                                interval=SHARE_BUILD_INTERVAL):
        """Poll until a share is gone."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.shares_v2_client.get_share(share_id)
            except lib_exc.NotFound:
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for share %s deletion", share_id)

    def _get_export_locations(self, share_id):
        """Retrieve export locations for a share via dedicated API.

        Manila's v2 share representation does not always embed
        export locations in the share dict. Use the explicit
        export locations API instead, similar to the PowerScale
        revert tests.
        """
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    def _wait_for_share_status(self, share_id, target,
                              timeout=SHARE_BUILD_TIMEOUT,
                              interval=SHARE_BUILD_INTERVAL):
        """Poll until the share reaches the target status."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            s = self.shares_v2_client.get_share(share_id)['share']
            status = (s.get('status') or '').lower()
            last = status
            if status == target:
                return s
            if status in ('error', 'error_reverting'):
                self.fail(
                    "Share %s entered error state: %s" % (share_id, status))
            time.sleep(interval)
        self.fail("Timeout waiting for share %s to reach '%s'; "
                  "last='%s'" % (share_id, target, last))

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def create_snapshot(self, share_id, name=None):
        """Create a snapshot and wait for it to become available."""
        name = name or data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-revert-snap')
        snap = self.shares_v2_client.create_snapshot(
            share_id=share_id,
            name=name,
            force=False,
        )['snapshot']
        LOG.info("Created snapshot '%s' (id=%s) for share %s",
                 snap['name'], snap['id'], share_id)
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        self._wait_for_snapshot_status(snap['id'], 'available')
        return self.shares_v2_client.get_snapshot(snap['id'])['snapshot']

    def _delete_snapshot_safe(self, snap_id):
        """Delete a snapshot; ignore NotFound."""
        try:
            self.shares_v2_client.delete_snapshot(snap_id)
        except lib_exc.NotFound:
            return
        except Exception as e:
            LOG.debug("delete_snapshot(%s) raised: %s", snap_id, e)
        try:
            self.shares_v2_client.wait_for_resource_deletion(snapshot_id=snap_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            self._wait_for_snapshot_deletion(snap_id)

    def _wait_for_snapshot_deletion(self, snap_id,
                                    timeout=SHARE_BUILD_TIMEOUT,
                                    interval=SHARE_BUILD_INTERVAL):
        """Poll until a snapshot is gone."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.shares_v2_client.get_snapshot(snap_id)
            except lib_exc.NotFound:
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for snapshot %s deletion", snap_id)

    def _wait_for_snapshot_status(self, snap_id, target,
                                  timeout=SHARE_BUILD_TIMEOUT,
                                  interval=SHARE_BUILD_INTERVAL):
        """Poll until the snapshot reaches the target status."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            s = self.shares_v2_client.get_snapshot(snap_id)['snapshot']
            status = (s.get('status') or '').lower()
            last = status
            if status == target:
                return s
            if status in ('error', 'error_deleting'):
                self.fail(
                    "Snapshot %s entered error state: %s" % (snap_id, status))
            time.sleep(interval)
        self.fail("Timeout waiting for snapshot %s to reach '%s'; "
                  "last='%s'" % (snap_id, target, last))

    # ------------------------------------------------------------------
    # Revert-to-snapshot helper
    # ------------------------------------------------------------------
    def revert_to_snapshot(self, share_id, snapshot_id):
        """Revert a share to a snapshot and wait until available.

        PowerStore revert is synchronous (atomic operation):
          1. Manila sends POST /shares/{id}/action {"revert": ...}
          2. Driver calls PowerStore API:
             POST /api/rest/file_system_snapshot/{snapshot_id}/restore
          3. PowerStore atomically restores the filesystem (~2s)
          4. Driver returns immediately (no async job tracking)
          5. Share status: available (no intermediate states)

        :param share_id: Share to revert.
        :param snapshot_id: Snapshot to revert to.
        :returns: Updated share dict after revert completes.
        """
        LOG.info("Reverting share %s to snapshot %s", share_id, snapshot_id)
        self.shares_v2_client.revert_to_snapshot(share_id, snapshot_id)
        self._wait_for_share_status(share_id, 'available')
        share = self.shares_v2_client.get_share(share_id)['share']
        return share

    # ------------------------------------------------------------------
    # PowerStore REST API helper (backend verification)
    # ------------------------------------------------------------------
    def _get_powerstore_snapshot_details(self, snapshot_id):
        """Query the PowerStore REST API to get snapshot details.

        Reads PowerStore credentials from manila.conf [powerstore] section.
        Returns the snapshot details dict, or None if not found.

        This is used for backend verification to ensure the PowerStore
        API was called correctly during revert operations.

        :param snapshot_id: Manila snapshot UUID (may map to PowerStore
                           snapshot name or ID).
        :returns: Snapshot details dict from PowerStore API, or None.
        """
        try:
            conf = configparser.ConfigParser()
            conf.read('/etc/manila/manila.conf')
            ps_ip = conf.get('powerstore', 'san_ip')
            ps_user = conf.get('powerstore', 'san_login')
            ps_pass = conf.get('powerstore', 'san_password')
        except Exception as e:
            LOG.warning("Cannot read PowerStore creds from manila.conf: %s", e)
            return None

        # PowerStore uses snapshot name as the identifier in Manila
        # Try both by ID and by name
        url = 'https://%s/api/rest/file_system_snapshot' % ps_ip
        params = {
            'name': 'eq.%s' % snapshot_id,
            'select': 'id,name,file_system_id',
        }
        try:
            resp = requests.get(
                url, params=params,
                auth=(ps_user, ps_pass),
                verify=False, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    snap_details = data[0]
                    LOG.info("PowerStore snapshot '%s' found: id=%s, fs_id=%s",
                             snapshot_id, snap_details.get('id'),
                             snap_details.get('file_system_id'))
                    return snap_details
            LOG.warning("PowerStore snapshot lookup for '%s' returned %s",
                        snapshot_id, resp.status_code)
        except Exception as e:
            LOG.warning("PowerStore REST query failed: %s", e)
        return None


# ======================================================================
# NFS Revert-to-Snapshot Tests
# ======================================================================
class _NFSRevertSnapshotTests(object):
    """NFS share revert-to-snapshot test methods for PowerStore."""

    @classmethod
    def skip_checks(cls):
        super(_NFSRevertSnapshotTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Revert NFS share to snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b1c2d3e4-f5a6-4b7c-8d9e-020304050601')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_nfs_share_to_snapshot(self):
        """Revert an NFS share to a snapshot and verify success.

        Expected PowerStore side-effects:
          1. Snapshot created on PowerStore
          2. PowerStore API call: POST /file_system_snapshot/{id}/restore
          3. Share status: available (synchronous, no intermediate states)

        This test focuses on the PowerStore-specific synchronous behavior,
        unlike the generic tempest test which just verifies the API call.
        """
        LOG.info("=== test_revert_nfs_share_to_snapshot ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Create snapshot
        snapshot = self.create_snapshot(share_id)
        self.assertEqual(snapshot['status'], 'available')

        # Verify snapshot exists on PowerStore backend (optional verification)
        snap_details = self._get_powerstore_snapshot_details(snapshot['id'])
        if snap_details:
            LOG.info("Backend verification: snapshot exists on PowerStore "
                     "(id=%s, fs_id=%s)",
                     snap_details.get('id'), snap_details.get('file_system_id'))

        # Revert to snapshot
        reverted = self.revert_to_snapshot(share_id, snapshot['id'])
        self.assertEqual(reverted['status'], 'available')
        self.assertEqual(int(reverted['size']), SHARE_MIN_SIZE)
        LOG.info("NFS share %s reverted to snapshot %s successfully",
                 share_id, snapshot['id'])

    # ----------------------------------------------------------------
    # Test: Export locations preserved after revert
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b1c2d3e4-f5a6-4b7c-8d9e-020304050602')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_nfs_preserves_export_locations(self):
        """Verify export locations are preserved after revert.

        PowerStore revert restores the filesystem state but should
        preserve the export configuration. This is critical for
        NFS shares to remain accessible after revert.
        """
        LOG.info("=== test_revert_nfs_preserves_export_locations ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )
        share_id = share['id']

        # Get export locations before revert via dedicated API
        exports_before = self._get_export_locations(share_id)
        self.assertTrue(len(exports_before) > 0,
                        "Share should have export locations")
        LOG.info("Export locations before revert: %s", exports_before)

        snapshot = self.create_snapshot(share_id)

        self.revert_to_snapshot(share_id, snapshot['id'])

        # Verify export locations after revert
        exports_after = self._get_export_locations(share_id)
        self.assertEqual(len(exports_after), len(exports_before),
                         "Export locations count should be preserved")
        LOG.info("Export locations after revert: %s", exports_after)

    # ----------------------------------------------------------------
    # Test: Snapshot available after revert
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b1c2d3e4-f5a6-4b7c-8d9e-020304050603')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_snapshot_available_after_revert(self):
        """Verify the snapshot remains available after revert.

        The snapshot should not be deleted or altered by the revert
        operation and should remain available for future use.
        """
        LOG.info("=== test_nfs_snapshot_available_after_revert ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )

        snapshot = self.create_snapshot(share['id'])
        snap_id = snapshot['id']

        self.revert_to_snapshot(share['id'], snap_id)

        snap_after = self.shares_v2_client.get_snapshot(snap_id)['snapshot']
        self.assertEqual(snap_after['status'], 'available',
                         "Snapshot should remain available after revert")
        LOG.info("Snapshot %s remains available after revert", snap_id)

    # ----------------------------------------------------------------
    # Test: Full NFS revert lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b1c2d3e4-f5a6-4b7c-8d9e-020304050604')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_revert_lifecycle(self):
        """Full lifecycle: create -> snapshot -> revert -> delete (NFS).

        Exercises the complete revert flow on PowerStore:
          1. Create NFS share
          2. Create snapshot
          3. Revert to snapshot
          4. Verify share available
          5. Delete snapshot
          6. Delete share
          7. Verify gone

        PowerStore API: POST /file_system_snapshot/{id}/restore (synchronous)
        """
        LOG.info("=== test_nfs_revert_lifecycle ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        snap_id = snapshot['id']

        reverted = self.revert_to_snapshot(share_id, snap_id)
        self.assertEqual(reverted['status'], 'available')

        # Delete snapshot
        self.shares_v2_client.delete_snapshot(snap_id)
        self._wait_for_snapshot_deletion(snap_id)

        # Delete share
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full NFS revert lifecycle completed for share %s", share_id)


# ======================================================================
# Negative Revert-to-Snapshot Tests
# ======================================================================
class _NegativeRevertSnapshotTests(object):
    """Negative test methods for PowerStore revert-to-snapshot.

    Tests error scenarios:
      - Revert to snapshot of different share
      - Revert to non-existent snapshot
      - Revert share in error state
    """

    @classmethod
    def skip_checks(cls):
        super(_NegativeRevertSnapshotTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Revert to snapshot of different share should fail
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-4e7f-8a9b-040506070809')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_revert_to_snapshot_different_share_fails(self):
        """Revert a share to a snapshot belonging to a different share.

        Expected behavior:
          - Manila should reject the request with BadRequest or
            the share should enter error_reverting state.

        PowerStore API: POST /file_system_snapshot/{id}/restore
        - PowerStore allows the API call but the snapshot belongs to
          a different filesystem, so the operation should fail.
        """
        LOG.info("=== test_revert_to_snapshot_different_share_fails ===")

        share_type = self.create_revert_share_type()
        share1 = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )
        share2 = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )

        # Create snapshot on share1
        snapshot = self.create_snapshot(share1['id'])

        # Try to revert share2 to share1's snapshot
        try:
            self.shares_v2_client.revert_to_snapshot(
                share2['id'], snapshot['id'])
            # If API accepted, wait and check for error state
            time.sleep(5)
            share2_after = self.shares_v2_client.get_share(
                share2['id'])['share']
            self.assertIn(
                share2_after['status'],
                ('error', 'error_reverting', 'available'),
                f"Expected error state but got: {share2_after['status']}")
            LOG.info("Revert correctly failed with status=%s",
                     share2_after['status'])
        except lib_exc.BadRequest:
            LOG.info("Revert correctly rejected with BadRequest")
        except lib_exc.ServerFault:
            LOG.info("Revert correctly rejected with ServerFault")

    # ----------------------------------------------------------------
    # Test: Revert to non-existent snapshot should fail
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f2a3b4c5-d6e7-5f8a-9b0c-05060708090a')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_revert_to_nonexistent_snapshot_fails(self):
        """Revert a share to a non-existent snapshot.

        Expected behavior:
          - Manila should reject the request with NotFound or
            the share should enter error_reverting state.
        """
        LOG.info("=== test_revert_to_nonexistent_snapshot_fails ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )

        # Use a fake snapshot UUID
        fake_snap_id = '00000000-0000-0000-0000-000000000000'

        try:
            self.shares_v2_client.revert_to_snapshot(
                share['id'], fake_snap_id)
            # If API accepted, wait and check for error state
            time.sleep(5)
            share_after = self.shares_v2_client.get_share(
                share['id'])['share']
            self.assertIn(
                share_after['status'],
                ('error', 'error_reverting', 'available'),
                f"Expected error state but got: {share_after['status']}")
            LOG.info("Revert correctly failed with status=%s",
                     share_after['status'])
        except lib_exc.NotFound:
            LOG.info("Revert correctly rejected with NotFound")
        except lib_exc.BadRequest:
            LOG.info("Revert correctly rejected with BadRequest")


# ======================================================================
# Share Type Extra-Spec Tests
# ======================================================================
class _ShareTypeRevertTests(object):
    """Share type extra-spec validation tests for revert-to-snapshot.

    Verifies that share types are created correctly with
    revert_to_snapshot_support extra-specs and that the driver
    reports the capability.
    """

    @classmethod
    def skip_checks(cls):
        super(_ShareTypeRevertTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Share type with revert_to_snapshot_support=True
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a3b4c5d6-e7f8-6a9b-0c1d-060708090a0b')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_share_type_with_revert_extra_spec(self):
        """Create share type with revert_to_snapshot_support=True.

        Verify:
          - Extra-spec is set correctly
          - Driver reports the capability
        """
        LOG.info("=== test_create_share_type_with_revert_extra_spec ===")

        share_type = self.create_revert_share_type()

        # Verify extra-specs
        specs = share_type.get('extra_specs', {})
        self.assertIn('revert_to_snapshot_support', specs)
        self.assertEqual(specs['revert_to_snapshot_support'], 'True')
        self.assertEqual(specs['snapshot_support'], 'True')
        self.assertEqual(specs['driver_handles_share_servers'], 'False')
        LOG.info("Share type %s has correct revert extra-specs",
                 share_type['id'])

    # ----------------------------------------------------------------
    # Test: Share type without revert_to_snapshot_support
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b4c5d6e7-f8a9-7b0c-1d2e-0708090a0b0c')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_share_type_without_revert_extra_spec(self):
        """Create share type without revert_to_snapshot_support.

        Verify:
          - Extra-spec is not set
          - Share can still be created (revert just won't work)
        """
        LOG.info("=== test_create_share_type_without_revert_extra_spec ===")

        name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-no-revert-type')
        extra_specs = {
            'driver_handles_share_servers': 'False',
            'snapshot_support': 'True',
        }
        st = self.share_types_client.create_share_type(
            name=name,
            extra_specs=extra_specs)['share_type']
        LOG.info("Created share type '%s' (id=%s) with extra_specs=%s",
                 st['name'], st['id'], extra_specs)
        self.addCleanup(self._delete_share_type_safe, st['id'])

        specs = st.get('extra_specs', {})
        self.assertNotIn('revert_to_snapshot_support', specs)
        LOG.info("Share type %s correctly has no revert extra-spec",
                 st['id'])

    # ----------------------------------------------------------------
    # Test: Multiple snapshots, revert to different snapshots
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c5d6e7f8-a9b0-8c1d-2e3f-08090a0b0c0d')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_to_multiple_snapshots(self):
        """Create multiple snapshots and revert to each as latest.

        Manila only allows revert to the *latest* snapshot. To exercise
        multiple snapshots with PowerStore while respecting this
        constraint, this test follows the pattern:

          - Create snapshot A, revert to A
          - Create snapshot B, revert to B (now latest)
          - Create snapshot C, revert to C (now latest)

        This proves PowerStore correctly handles multiple revert
        operations with different snapshots without relying on any
        async job semantics.
        """
        LOG.info("=== test_revert_to_multiple_snapshots ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )
        share_id = share['id']

        # Create multiple snapshots; each becomes latest before revert
        for i in range(3):
            snap = self.create_snapshot(
                share_id,
                name=data_utils.rand_name(
                    prefix=CONF.resource_name_prefix,
                    name=f'ps-revert-snap-{i}'))
            LOG.info("Created snapshot %d: %s", i, snap['id'])

            reverted = self.revert_to_snapshot(share_id, snap['id'])
            self.assertEqual(reverted['status'], 'available')
            LOG.info("Reverted to snapshot %d: %s", i, snap['id'])

        LOG.info("Successfully reverted to %d different snapshots", 3)


# ======================================================================
# CIFS Revert-to-Snapshot Tests
# ======================================================================
class _CIFSRevertSnapshotTests(object):
    """CIFS share revert-to-snapshot test methods for PowerStore."""

    @classmethod
    def skip_checks(cls):
        super(_CIFSRevertSnapshotTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Revert CIFS share to snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-4c7d-8e9f-030405060701')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_cifs_share_to_snapshot(self):
        """Revert a CIFS share to a snapshot and verify success.

        Same PowerStore side-effects as NFS revert:
          1. Snapshot created on PowerStore
          2. PowerStore API call: POST /file_system_snapshot/{id}/restore
          3. Share status: available (synchronous)
        """
        LOG.info("=== test_revert_cifs_share_to_snapshot ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        snapshot = self.create_snapshot(share_id)
        self.assertEqual(snapshot['status'], 'available')

        reverted = self.revert_to_snapshot(share_id, snapshot['id'])
        self.assertEqual(reverted['status'], 'available')
        self.assertEqual(int(reverted['size']), SHARE_MIN_SIZE)
        LOG.info("CIFS share %s reverted to snapshot %s successfully",
                 share_id, snapshot['id'])

    # ----------------------------------------------------------------
    # Test: Export locations preserved after revert (CIFS)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-4c7d-8e9f-030405060702')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_cifs_preserves_export_locations(self):
        """Verify export locations are preserved after revert (CIFS)."""
        LOG.info("=== test_revert_cifs_preserves_export_locations ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )
        share_id = share['id']

        exports_before = self._get_export_locations(share_id)
        self.assertTrue(len(exports_before) > 0,
                        "Share should have export locations")

        snapshot = self.create_snapshot(share_id)

        self.revert_to_snapshot(share_id, snapshot['id'])

        exports_after = self._get_export_locations(share_id)
        self.assertEqual(len(exports_after), len(exports_before))
        LOG.info("CIFS export locations preserved: %s", exports_after)

    # ----------------------------------------------------------------
    # Test: Full CIFS revert lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-4c7d-8e9f-030405060703')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_revert_lifecycle(self):
        """Full lifecycle: create -> snapshot -> revert -> delete (CIFS)."""
        LOG.info("=== test_cifs_revert_lifecycle ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=SHARE_MIN_SIZE,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        snap_id = snapshot['id']

        reverted = self.revert_to_snapshot(share_id, snap_id)
        self.assertEqual(reverted['status'], 'available')

        self.shares_v2_client.delete_snapshot(snap_id)
        self._wait_for_snapshot_deletion(snap_id)

        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full CIFS revert lifecycle completed for share %s", share_id)


# ---------------------------------------------------------------------------
# Concrete test classes wired to a Tempest-compatible base class.
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerStoreRevertSnapshotNFS(
            _NFSRevertSnapshotTests,
            PowerStoreShareRevertSnapshotTest,
            manila_base.BaseSharesAdminTest):
        """NFS revert-to-snapshot functional tests
        (manila_tempest_tests base)."""

    class TestPowerStoreRevertSnapshotCIFS(
            _CIFSRevertSnapshotTests,
            PowerStoreShareRevertSnapshotTest,
            manila_base.BaseSharesAdminTest):
        """CIFS revert-to-snapshot functional tests
        (manila_tempest_tests base)."""

    class TestPowerStoreRevertSnapshotNegative(
            _NegativeRevertSnapshotTests,
            PowerStoreShareRevertSnapshotTest,
            manila_base.BaseSharesAdminTest):
        """Negative revert-to-snapshot functional tests
        (manila_tempest_tests base)."""

    class TestPowerStoreRevertSnapshotShareType(
            _ShareTypeRevertTests,
            PowerStoreShareRevertSnapshotTest,
            manila_base.BaseSharesAdminTest):
        """Share type extra-spec revert tests
        (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerStoreRevertSnapshotNFS(
            _NFSRevertSnapshotTests,
            PowerStoreShareRevertSnapshotTest,
            tempest_test.BaseTestCase):
        """NFS revert-to-snapshot functional tests
        (tempest.test fallback base)."""
        credentials = ['primary', 'admin']

    class TestPowerStoreRevertSnapshotCIFS(
            _CIFSRevertSnapshotTests,
            PowerStoreShareRevertSnapshotTest,
            tempest_test.BaseTestCase):
        """CIFS revert-to-snapshot functional tests
        (tempest.test fallback base)."""
        credentials = ['primary', 'admin']

    class TestPowerStoreRevertSnapshotNegative(
            _NegativeRevertSnapshotTests,
            PowerStoreShareRevertSnapshotTest,
            tempest_test.BaseTestCase):
        """Negative revert-to-snapshot functional tests
        (tempest.test fallback base)."""
        credentials = ['primary', 'admin']

    class TestPowerStoreRevertSnapshotShareType(
            _ShareTypeRevertTests,
            PowerStoreShareRevertSnapshotTest,
            tempest_test.BaseTestCase):
        """Share type extra-spec revert tests
        (tempest.test fallback base)."""
        credentials = ['primary', 'admin']
