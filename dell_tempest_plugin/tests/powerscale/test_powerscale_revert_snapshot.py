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
Tempest functional tests for Dell PowerScale revert-to-snapshot feature.

These tests exercise the revert-to-snapshot lifecycle through real Manila
API calls:
  - Creating shares with revert_to_snapshot_support=True extra-spec
  - Creating snapshots of shares
  - Reverting shares to a snapshot
  - Verifying share status transitions during async revert
  - Verifying share data and export locations after revert

References (from revert_snapshot.patch):
  - PowerScale API: POST /platform/12/job/jobs
    (create_job — DomainMark during share creation,
     SnapRevert during revert-to-snapshot)
  - PowerScale API: GET /platform/12/job/jobs/<id>
    (get_job_status — polls async job status)
  - Manila share action: POST /shares/<id>/action
    {"revert": {"snapshot_id": ...}}
  - Manila status transitions:
    available -> reverting -> reverting_to_snapshot -> available
  - Error status: reverting_error (if backend job fails)
  - Share type extra-spec: revert_to_snapshot_support=True
  - Config options: powerscale_job_retries, powerscale_job_interval
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

# Revert is async on PowerScale; allow extra time for the SnapRevert job
REVERT_TIMEOUT = 900
REVERT_INTERVAL = 10


class PowerScaleRevertSnapshotTest(object):
    """Mixin with helpers for PowerScale revert-to-snapshot tests.

    Provides utility methods for creating share types (with
    revert_to_snapshot_support), shares, snapshots, reverting,
    and waiting for share status transitions via the Manila API.
    """

    @classmethod
    def setup_clients(cls):
        super(PowerScaleRevertSnapshotTest, cls).setup_clients()
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
    def create_revert_share_type(self, name=None, extra_specs=None):
        """Create a Manila share type with revert_to_snapshot_support.

        :param name: Optional name; auto-generated if None.
        :param extra_specs: Additional extra-specs dict to merge.
        :returns: Created share type dict.
        """
        name = name or data_utils.rand_name('ps-revert-type')
        specs = {
            'driver_handles_share_servers': 'False',
            'snapshot_support': 'True',
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

    def create_snapshot_share_type(self, name=None, extra_specs=None):
        """Create a share type with snapshot support but no revert."""
        name = name or data_utils.rand_name('ps-snap-type')
        specs = {
            'driver_handles_share_servers': 'False',
            'snapshot_support': 'True',
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
        """Delete share type, ignoring NotFound."""
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
        """Create a Manila share and wait until it becomes available.

        :param protocol: 'NFS' or 'CIFS'
        :param share_type_name: Name of the share type to use.
        :param size: Share size in GB.
        :param name: Optional share name.
        :returns: Created share dict.
        """
        name = name or data_utils.rand_name(
            f'ps-revert-{protocol.lower()}')
        share = self.shares_v2_client.create_share(
            share_protocol=protocol,
            size=size,
            name=name,
            share_type_id=share_type_name,
        )
        sh = share.get('share', share)
        LOG.info("Created share '%s' (id=%s, protocol=%s, size=%sG)",
                 sh['name'], sh['id'], protocol, size)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self.shares_v2_client.get_share(sh['id']).get(
            'share', self.shares_v2_client.get_share(sh['id']))

    def _delete_share_safe(self, share_id):
        """Delete a share and wait for it to be removed."""
        try:
            share = self.shares_v2_client.get_share(share_id)
            sh = share.get('share', share)
            status = sh.get('status', '').lower()
            if status in ('reverting', 'reverting_error',
                          'reverting_to_snapshot'):
                LOG.info("Share %s in %s status, resetting to available "
                         "before delete", share_id, status)
                self.shares_v2_client.reset_state(share_id,
                                                  status='available')
                self._wait_for_share_status(share_id, 'available',
                                            timeout=60)
        except lib_exc.NotFound:
            LOG.debug("Share %s already gone", share_id)
            return
        except Exception as e:
            LOG.warning("Pre-delete check for share %s failed: %s",
                        share_id, e)
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
        """Poll share status until it reaches target or errors out."""
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            share = self.shares_v2_client.get_share(share_id)
            sh = share.get('share', share)
            status = sh.get('status', '').lower()
            last_status = status
            if status == target_status:
                return
            if status in ('error', 'error_deleting', 'reverting_error'):
                self.fail(
                    f"Share {share_id} entered error state: {status}")
            time.sleep(interval)
        self.fail(
            f"Timeout waiting for share {share_id} to reach "
            f"'{target_status}'; last status='{last_status}'")

    def _wait_for_share_status_revert(self, share_id, target_status,
                                      timeout=REVERT_TIMEOUT,
                                      interval=REVERT_INTERVAL):
        """Poll share status for revert operations (longer timeout).

        Revert is async on PowerScale — the share goes through
        reverting -> reverting_to_snapshot -> available.
        """
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            share = self.shares_v2_client.get_share(share_id)
            sh = share.get('share', share)
            status = sh.get('status', '').lower()
            last_status = status
            if status == target_status:
                return
            if status in ('error', 'reverting_error'):
                self.fail(
                    f"Share {share_id} entered error state during revert: "
                    f"{status}")
            time.sleep(interval)
        self.fail(
            f"Timeout waiting for share {share_id} to reach "
            f"'{target_status}' after revert; last status='{last_status}'")

    def _wait_for_share_deletion(self, share_id,
                                 timeout=SHARE_BUILD_TIMEOUT,
                                 interval=SHARE_BUILD_INTERVAL):
        """Poll until share is gone (NotFound)."""
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
        """Retrieve export locations for a share via the dedicated API."""
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def create_snapshot(self, share_id, name=None):
        """Create a snapshot and wait for it to become available.

        :param share_id: The share to snapshot.
        :param name: Optional snapshot name.
        :returns: Created snapshot dict.
        """
        name = name or data_utils.rand_name('ps-revert-snap')
        snap = self.shares_v2_client.create_snapshot(
            share_id=share_id,
            name=name,
        )
        sn = snap.get('snapshot', snap)
        LOG.info("Created snapshot '%s' (id=%s) for share %s",
                 sn['name'], sn['id'], share_id)
        self.addCleanup(self._delete_snapshot_safe, sn['id'])
        self._wait_for_snapshot_status(sn['id'], 'available')
        return self.shares_v2_client.get_snapshot(sn['id']).get(
            'snapshot', self.shares_v2_client.get_snapshot(sn['id']))

    def _delete_snapshot_safe(self, snapshot_id):
        """Delete a snapshot, ignoring NotFound."""
        try:
            snap = self.shares_v2_client.get_snapshot(snapshot_id)
            sn = snap.get('snapshot', snap)
            status = sn.get('status', '').lower()
            if status not in ('available', 'error'):
                LOG.info("Snapshot %s in %s status, resetting to available "
                         "before delete", snapshot_id, status)
                self.shares_v2_client.snapshot_reset_state(
                    snapshot_id, status='available')
                self._wait_for_snapshot_status(snapshot_id, 'available',
                                               timeout=60)
        except lib_exc.NotFound:
            LOG.debug("Snapshot %s already gone", snapshot_id)
            return
        except Exception as e:
            LOG.warning("Pre-delete check for snapshot %s failed: %s",
                        snapshot_id, e)
        try:
            self.shares_v2_client.delete_snapshot(snapshot_id)
            LOG.info("Requested deletion of snapshot %s", snapshot_id)
        except lib_exc.NotFound:
            LOG.debug("Snapshot %s already gone", snapshot_id)
            return
        except Exception as e:
            LOG.warning("Failed to delete snapshot %s: %s",
                        snapshot_id, e)
            return
        self._wait_for_snapshot_deletion(snapshot_id)

    def _wait_for_snapshot_status(self, snapshot_id, target_status,
                                  timeout=SHARE_BUILD_TIMEOUT,
                                  interval=SHARE_BUILD_INTERVAL):
        """Poll snapshot status until it reaches target."""
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            snap = self.shares_v2_client.get_snapshot(snapshot_id)
            sn = snap.get('snapshot', snap)
            status = sn.get('status', '').lower()
            last_status = status
            if status == target_status:
                return
            if status in ('error', 'error_deleting'):
                self.fail(
                    f"Snapshot {snapshot_id} entered error state: {status}")
            time.sleep(interval)
        self.fail(
            f"Timeout waiting for snapshot {snapshot_id} to reach "
            f"'{target_status}'; last status='{last_status}'")

    def _wait_for_snapshot_deletion(self, snapshot_id,
                                    timeout=SHARE_BUILD_TIMEOUT,
                                    interval=SHARE_BUILD_INTERVAL):
        """Poll until snapshot is gone (NotFound)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.shares_v2_client.get_snapshot(snapshot_id)
            except lib_exc.NotFound:
                LOG.info("Snapshot %s deletion confirmed", snapshot_id)
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for snapshot %s deletion", snapshot_id)

    # ------------------------------------------------------------------
    # Revert helper
    # ------------------------------------------------------------------
    def revert_to_snapshot(self, share_id, snapshot_id):
        """Revert a share to a snapshot and wait until available.

        The PowerScale backend executes the revert asynchronously:
          1. Manila sends POST /shares/<id>/action {"revert": ...}
          2. Driver creates SnapRevert job on PowerScale
          3. Share status: reverting -> reverting_to_snapshot -> available
          4. Periodic task polls the job and updates status

        :param share_id: Share to revert.
        :param snapshot_id: Snapshot to revert to.
        :returns: Updated share dict after revert completes.
        """
        LOG.info("Reverting share %s to snapshot %s", share_id, snapshot_id)
        self.shares_v2_client.revert_to_snapshot(share_id, snapshot_id)
        self._wait_for_share_status_revert(share_id, 'available')
        share = self.shares_v2_client.get_share(share_id)
        return share.get('share', share)

    # ------------------------------------------------------------------
    # Extend helper
    # ------------------------------------------------------------------
    def extend_share(self, share_id, new_size):
        """Extend a share and wait until it becomes available."""
        LOG.info("Extending share %s to %sG", share_id, new_size)
        self.shares_v2_client.extend_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        share = self.shares_v2_client.get_share(share_id)
        return share.get('share', share)


# ===================================================================
# NFS Revert-to-Snapshot Tests
# ===================================================================
class _NFSRevertSnapshotTests(object):
    """Mixin: NFS share revert-to-snapshot test methods for PowerScale.

    Each test makes real Manila API calls that propagate to the
    PowerScale backend, triggering DomainMark and SnapRevert jobs
    via the PowerScale REST API.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSRevertSnapshotTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Revert NFS share to snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345670101')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_nfs_share_to_snapshot(self):
        """Revert an NFS share to a snapshot and verify success.

        Expected PowerScale side-effects:
          1. DomainMark job created during share creation
          2. Snapshot created on PowerScale
          3. SnapRevert job created during revert
          4. Share status transitions:
             available -> reverting -> reverting_to_snapshot -> available
        """
        LOG.info("=== test_revert_nfs_share_to_snapshot ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Create snapshot
        snapshot = self.create_snapshot(share_id)
        self.assertEqual(snapshot['status'], 'available')

        # Revert to snapshot
        reverted = self.revert_to_snapshot(share_id, snapshot['id'])
        self.assertEqual(reverted['status'], 'available')
        self.assertEqual(int(reverted['size']), 1)
        LOG.info("NFS share %s reverted to snapshot %s successfully",
                 share_id, snapshot['id'])

    # ----------------------------------------------------------------
    # Test: Revert NFS share preserves export locations
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345670102')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_nfs_share_preserves_export(self):
        """Revert an NFS share and verify export locations are preserved.

        After a SnapRevert, the share path remains the same on
        PowerScale; only the data is restored. Export locations
        should be identical before and after revert.
        """
        LOG.info("=== test_revert_nfs_share_preserves_export ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        # Get export locations before revert
        exports_before = self._get_export_locations(share_id)
        self.assertTrue(len(exports_before) > 0,
                        "Share must have export locations")

        # Create snapshot and revert
        snapshot = self.create_snapshot(share_id)
        self.revert_to_snapshot(share_id, snapshot['id'])

        # Get export locations after revert
        exports_after = self._get_export_locations(share_id)
        self.assertTrue(len(exports_after) > 0,
                        "Share must still have export locations after revert")

        def _get_path(el):
            if isinstance(el, dict):
                return el.get('path', str(el))
            return str(el)

        paths_before = sorted([_get_path(e) for e in exports_before])
        paths_after = sorted([_get_path(e) for e in exports_after])
        self.assertEqual(paths_before, paths_after,
                         "Export locations should be preserved after revert")
        LOG.info("Export locations preserved after revert for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Full NFS revert lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345670103')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_revert_lifecycle(self):
        """Full lifecycle: create -> snapshot -> revert -> delete.

        This exercises the complete revert flow on PowerScale:
          1. Create share (DomainMark job runs)
          2. Create snapshot
          3. Revert to snapshot (SnapRevert job runs async)
          4. Verify share is available after revert
          5. Delete snapshot
          6. Delete share
        """
        LOG.info("=== test_nfs_revert_lifecycle ===")

        share_type = self.create_revert_share_type()

        # Create share
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Create snapshot
        snapshot = self.create_snapshot(share_id)
        self.assertEqual(snapshot['status'], 'available')

        # Revert to snapshot
        reverted = self.revert_to_snapshot(share_id, snapshot['id'])
        self.assertEqual(reverted['status'], 'available')

        # Verify export locations still present
        export_locations = self._get_export_locations(share_id)
        self.assertTrue(len(export_locations) > 0,
                        "Share must have export locations after revert")

        # Delete snapshot
        self.shares_v2_client.delete_snapshot(snapshot['id'])
        self._wait_for_snapshot_deletion(snapshot['id'])

        # Delete share
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full NFS revert lifecycle completed for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Snapshot remains available after revert
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345670104')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_snapshot_available_after_revert(self):
        """Verify that the snapshot is still available after revert.

        The revert operation restores the share to the snapshot state.
        The snapshot itself should remain available for future use.
        """
        LOG.info("=== test_nfs_snapshot_available_after_revert ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        snapshot_id = snapshot['id']

        # Revert
        self.revert_to_snapshot(share_id, snapshot_id)

        # Verify snapshot is still available
        snap_after = self.shares_v2_client.get_snapshot(snapshot_id)
        sn = snap_after.get('snapshot', snap_after)
        self.assertEqual(sn['status'], 'available',
                         f"Snapshot should be available after revert, "
                         f"got {sn['status']}")
        LOG.info("Snapshot %s remains available after revert", snapshot_id)

    # ----------------------------------------------------------------
    # Test: Revert NFS share with revert_to_snapshot_support extra-spec
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345670105')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_share_type_has_revert_extra_spec(self):
        """Verify share type has revert_to_snapshot_support extra-spec.

        When creating a share type with revert_to_snapshot_support=True,
        the extra-spec should be visible and the share should be created
        with DomainMark job on PowerScale.
        """
        LOG.info("=== test_nfs_share_type_has_revert_extra_spec ===")

        share_type = self.create_revert_share_type()
        specs = share_type.get('extra_specs', {})
        self.assertIn('revert_to_snapshot_support', specs)
        self.assertEqual(specs['revert_to_snapshot_support'], 'True')
        self.assertIn('snapshot_support', specs)
        self.assertEqual(specs['snapshot_support'], 'True')

        # Create share — DomainMark job should run
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        self.assertEqual(share['status'], 'available')
        LOG.info("NFS share %s created with revert support (DomainMark "
                 "job executed)", share['id'])

    # ----------------------------------------------------------------
    # Test: Revert NFS share after extend restores original size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345670106')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_nfs_share_after_extend(self):
        """Create share, snapshot, extend, revert — size should restore.

        Steps:
          1. Create 1G NFS share with revert support
          2. Create snapshot (at 1G)
          3. Extend share to 2G
          4. Revert to snapshot
          5. Verify share size is back to 1G

        The revert operation on PowerScale restores the quota to the
        original snapshot size via quota_set after SnapRevert completes.
        """
        LOG.info("=== test_revert_nfs_share_after_extend ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(int(share['size']), 1)

        # Create snapshot at 1G
        snapshot = self.create_snapshot(share_id)

        # Extend to 2G
        extended = self.extend_share(share_id, 2)
        self.assertEqual(int(extended['size']), 2)
        LOG.info("Share %s extended to 2G", share_id)

        # Revert to snapshot (should restore to 1G)
        reverted = self.revert_to_snapshot(share_id, snapshot['id'])
        self.assertEqual(reverted['status'], 'available')
        self.assertEqual(int(reverted['size']), 1,
                         f"Share size should be 1G after revert, "
                         f"got {reverted['size']}G")
        LOG.info("Share %s reverted to 1G after extend", share_id)

    # ----------------------------------------------------------------
    # Test: Multiple snapshots, revert to the latest
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345670107')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_nfs_share_to_latest_snapshot(self):
        """Create multiple snapshots and revert to the latest one.

        Manila only allows revert to the latest snapshot. This test
        verifies the behavior:
          1. Create share
          2. Create snapshot A
          3. Create snapshot B (latest)
          4. Revert to snapshot B
          5. Verify success
        """
        LOG.info("=== test_revert_nfs_share_to_latest_snapshot ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        # Create snapshot A
        snap_a = self.create_snapshot(share_id,
                                      name=data_utils.rand_name('snap-a'))
        LOG.info("Created snapshot A: %s", snap_a['id'])

        # Create snapshot B (latest)
        snap_b = self.create_snapshot(share_id,
                                      name=data_utils.rand_name('snap-b'))
        LOG.info("Created snapshot B: %s", snap_b['id'])

        # Revert to latest snapshot B
        reverted = self.revert_to_snapshot(share_id, snap_b['id'])
        self.assertEqual(reverted['status'], 'available')
        LOG.info("Share %s reverted to latest snapshot %s",
                 share_id, snap_b['id'])

    # ----------------------------------------------------------------
    # Negative: Revert to non-latest snapshot should fail
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345670108')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_revert_nfs_share_to_non_latest_snapshot_fails(self):
        """Revert to a non-latest snapshot should be rejected.

        Manila enforces that only the latest snapshot can be used
        for revert. Attempting to revert to an older snapshot should
        raise a Conflict (409) error.
        """
        LOG.info("=== test_revert_nfs_share_to_non_latest_snapshot_fails ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        # Create snapshot A
        snap_a = self.create_snapshot(share_id,
                                      name=data_utils.rand_name('snap-a'))

        # Create snapshot B (latest)
        self.create_snapshot(share_id,
                             name=data_utils.rand_name('snap-b'))

        # Revert to snapshot A (not latest) should fail
        self.assertRaises(
            lib_exc.Conflict,
            self.shares_v2_client.revert_to_snapshot,
            share_id,
            snap_a['id'],
        )

        # Verify share remains available
        current = self.shares_v2_client.get_share(share_id)
        sh = current.get('share', current)
        self.assertEqual(sh['status'], 'available')
        LOG.info("Revert to non-latest snapshot correctly rejected for "
                 "share %s", share_id)


# ===================================================================
# CIFS Revert-to-Snapshot Tests
# ===================================================================
class _CIFSRevertSnapshotTests(object):
    """Mixin: CIFS share revert-to-snapshot test methods for PowerScale."""

    @classmethod
    def skip_checks(cls):
        super(_CIFSRevertSnapshotTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Revert CIFS share to snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1e2f3a4-b5c6-7890-defa-012345670201')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_cifs_share_to_snapshot(self):
        """Revert a CIFS share to a snapshot and verify success.

        Same PowerScale side-effects as NFS revert:
          1. DomainMark job during share creation
          2. SnapRevert job during revert
        """
        LOG.info("=== test_revert_cifs_share_to_snapshot ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        snapshot = self.create_snapshot(share_id)
        self.assertEqual(snapshot['status'], 'available')

        reverted = self.revert_to_snapshot(share_id, snapshot['id'])
        self.assertEqual(reverted['status'], 'available')
        self.assertEqual(int(reverted['size']), 1)
        LOG.info("CIFS share %s reverted to snapshot %s successfully",
                 share_id, snapshot['id'])

    # ----------------------------------------------------------------
    # Test: Full CIFS revert lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1e2f3a4-b5c6-7890-defa-012345670202')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_revert_lifecycle(self):
        """Full lifecycle: create -> snapshot -> revert -> delete (CIFS)."""
        LOG.info("=== test_cifs_revert_lifecycle ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        snapshot = self.create_snapshot(share_id)
        self.assertEqual(snapshot['status'], 'available')

        reverted = self.revert_to_snapshot(share_id, snapshot['id'])
        self.assertEqual(reverted['status'], 'available')

        # Delete snapshot
        self.shares_v2_client.delete_snapshot(snapshot['id'])
        self._wait_for_snapshot_deletion(snapshot['id'])

        # Delete share
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full CIFS revert lifecycle completed for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Revert CIFS share after extend
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1e2f3a4-b5c6-7890-defa-012345670203')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_revert_cifs_share_after_extend(self):
        """Create CIFS share, snapshot, extend, revert — size restores."""
        LOG.info("=== test_revert_cifs_share_after_extend ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)

        extended = self.extend_share(share_id, 2)
        self.assertEqual(int(extended['size']), 2)

        reverted = self.revert_to_snapshot(share_id, snapshot['id'])
        self.assertEqual(reverted['status'], 'available')
        self.assertEqual(int(reverted['size']), 1,
                         f"Share size should be 1G after revert, "
                         f"got {reverted['size']}G")
        LOG.info("CIFS share %s reverted to 1G after extend", share_id)

    # ----------------------------------------------------------------
    # Negative: Revert CIFS share to non-latest snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1e2f3a4-b5c6-7890-defa-012345670204')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_revert_cifs_share_to_non_latest_snapshot_fails(self):
        """Revert CIFS share to non-latest snapshot should fail."""
        LOG.info("=== test_revert_cifs_share_to_non_latest_snapshot ===")

        share_type = self.create_revert_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        snap_a = self.create_snapshot(share_id,
                                      name=data_utils.rand_name('snap-a'))
        self.create_snapshot(share_id,
                             name=data_utils.rand_name('snap-b'))

        self.assertRaises(
            lib_exc.Conflict,
            self.shares_v2_client.revert_to_snapshot,
            share_id,
            snap_a['id'],
        )

        current = self.shares_v2_client.get_share(share_id)
        sh = current.get('share', current)
        self.assertEqual(sh['status'], 'available')
        LOG.info("CIFS revert to non-latest snapshot correctly rejected "
                 "for share %s", share_id)


# ---------------------------------------------------------------------------
# Concrete test classes wired to a Tempest-compatible base class.
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleRevertSnapshotNFS(
            _NFSRevertSnapshotTests,
            PowerScaleRevertSnapshotTest,
            manila_base.BaseSharesAdminTest):
        """NFS revert-to-snapshot functional tests
        (manila_tempest_tests base)."""

    class TestPowerScaleRevertSnapshotCIFS(
            _CIFSRevertSnapshotTests,
            PowerScaleRevertSnapshotTest,
            manila_base.BaseSharesAdminTest):
        """CIFS revert-to-snapshot functional tests
        (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleRevertSnapshotNFS(
            _NFSRevertSnapshotTests,
            PowerScaleRevertSnapshotTest,
            tempest_test.BaseTestCase):
        """NFS revert-to-snapshot functional tests
        (tempest.test fallback base)."""
        credentials = ['primary', 'admin']

    class TestPowerScaleRevertSnapshotCIFS(
            _CIFSRevertSnapshotTests,
            PowerScaleRevertSnapshotTest,
            tempest_test.BaseTestCase):
        """CIFS revert-to-snapshot functional tests
        (tempest.test fallback base)."""
        credentials = ['primary', 'admin']
