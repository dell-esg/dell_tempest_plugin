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
Tempest functional tests for Dell PowerScale manage snapshot feature.

These tests exercise the manage/unmanage snapshot lifecycle through real
Manila API calls:
  - Creating shares and snapshots
  - Unmanaging snapshots (removing from Manila without deleting on backend)
  - Managing snapshots back into Manila
  - Verifying snapshot attributes after manage
  - Negative cases for invalid provider_location

References (from powerscale.py manage_existing_snapshot):
  - PowerScale API: GET /platform/1/snapshot/snapshots/<id>
    (get_snapshot_id — verifies snapshot exists on backend)
  - Manila manage snapshot: POST /snapshots/manage
    {"snapshot": {"share_id": ..., "provider_location": ...,
     "driver_options": {"size": N}}}
  - Manila unmanage snapshot: POST /snapshots/<id>/action
    {"unmanage": {}}
  - Snapshot status transitions:
    manage: manage_starting -> available
    unmanage: available -> unmanage_starting -> (removed from Manila)
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


class PowerScaleManageSnapshotTest(object):
    """Mixin with helpers for PowerScale manage snapshot tests.

    Provides utility methods for creating share types, shares,
    snapshots, managing, unmanaging, and waiting for status
    transitions via the Manila API.
    """

    @classmethod
    def setup_clients(cls):
        super(PowerScaleManageSnapshotTest, cls).setup_clients()
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
    def create_share_type(self, name=None, extra_specs=None):
        """Create a Manila share type with snapshot_support.

        :param name: Optional name; auto-generated if None.
        :param extra_specs: Additional extra-specs dict to merge.
        :returns: Created share type dict.
        """
        name = name or data_utils.rand_name('ps-manage-snap-type')
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
            f'ps-mgsnap-{protocol.lower()}')
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
            if status not in ('available', 'error', 'creating',
                              'deleting', 'error_deleting'):
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
            if status in ('error', 'error_deleting'):
                self.fail(
                    f"Share {share_id} entered error state: {status}")
            time.sleep(interval)
        self.fail(
            f"Timeout waiting for share {share_id} to reach "
            f"'{target_status}'; last status='{last_status}'")

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
        name = name or data_utils.rand_name('ps-mgsnap-snap')
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
            if status in ('error', 'error_deleting',
                          'manage_error'):
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
    # Manage / Unmanage snapshot helpers
    # ------------------------------------------------------------------
    def unmanage_snapshot(self, snapshot_id):
        """Unmanage a snapshot and wait until it disappears from Manila.

        The snapshot is removed from Manila's database but remains
        on the PowerScale backend.

        :param snapshot_id: ID of the snapshot to unmanage.
        """
        LOG.info("Unmanaging snapshot %s", snapshot_id)
        self.shares_v2_client.unmanage_snapshot(snapshot_id)
        self._wait_for_snapshot_deletion(snapshot_id)
        LOG.info("Snapshot %s unmanaged successfully", snapshot_id)

    def manage_snapshot(self, share_id, provider_location,
                        name=None, driver_options=None):
        """Manage an existing snapshot into Manila and wait for available.

        This calls the Manila manage snapshot API which triggers
        PowerScale's manage_existing_snapshot, which:
          1. GET /platform/1/snapshot/snapshots/<provider_location>
             to verify the snapshot exists on backend
          2. Validates snapshot path matches the share container path
          3. Returns provider_location and size

        :param share_id: ID of the share the snapshot belongs to.
        :param provider_location: The PowerScale snapshot ID.
        :param name: Optional snapshot name.
        :param driver_options: Optional dict (e.g., {"size": N}).
        :returns: Managed snapshot dict.
        """
        name = name or data_utils.rand_name('ps-managed-snap')
        snap = self.shares_v2_client.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
            name=name,
            driver_options=driver_options,
        )
        sn = snap.get('snapshot', snap)
        LOG.info("Manage snapshot request accepted: id=%s, "
                 "provider_location=%s, share_id=%s",
                 sn['id'], provider_location, share_id)
        self.addCleanup(self._delete_snapshot_safe, sn['id'])
        self._wait_for_snapshot_status(sn['id'], 'available')
        managed = self.shares_v2_client.get_snapshot(sn['id'])
        return managed.get('snapshot', managed)

    def _wait_for_snapshot_manage_error(self, snapshot_id,
                                        timeout=SHARE_BUILD_TIMEOUT,
                                        interval=SHARE_BUILD_INTERVAL):
        """Wait for snapshot to enter manage_error state."""
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            snap = self.shares_v2_client.get_snapshot(snapshot_id)
            sn = snap.get('snapshot', snap)
            status = sn.get('status', '').lower()
            last_status = status
            if status == 'manage_error':
                return
            if status == 'available':
                self.fail(
                    f"Snapshot {snapshot_id} unexpectedly became available")
            time.sleep(interval)
        self.fail(
            f"Timeout waiting for snapshot {snapshot_id} to reach "
            f"'manage_error'; last status='{last_status}'")

    def _cleanup_manage_error_snapshot(self, snapshot_id):
        """Clean up a snapshot that is in manage_error state."""
        try:
            snap = self.shares_v2_client.get_snapshot(snapshot_id)
            sn = snap.get('snapshot', snap)
            status = sn.get('status', '').lower()
            if status == 'manage_error':
                self.shares_v2_client.snapshot_reset_state(
                    snapshot_id, status='error')
                time.sleep(2)
            self.shares_v2_client.delete_snapshot(snapshot_id)
            self._wait_for_snapshot_deletion(snapshot_id)
        except lib_exc.NotFound:
            LOG.debug("Snapshot %s already gone", snapshot_id)
        except Exception as e:
            LOG.warning("Cleanup of manage_error snapshot %s failed: %s",
                        snapshot_id, e)


# ===================================================================
# NFS Manage Snapshot Tests
# ===================================================================
class _NFSManageSnapshotTests(object):
    """Mixin: NFS manage snapshot test methods for PowerScale.

    Each test makes real Manila API calls that propagate to the
    PowerScale backend, triggering get_snapshot_id via the
    PowerScale REST API.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSManageSnapshotTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Manage NFS snapshot after unmanage
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-7890-ef01-234567890101')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_nfs_snapshot_after_unmanage(self):
        """Unmanage an NFS snapshot, then manage it back.

        Steps:
          1. Create NFS share with snapshot_support
          2. Create snapshot
          3. Record provider_location (PowerScale snapshot ID)
          4. Unmanage snapshot (removed from Manila, stays on backend)
          5. Manage snapshot back using provider_location
          6. Verify managed snapshot is available

        Expected PowerScale side-effects on manage:
          - GET /platform/1/snapshot/snapshots/<provider_location>
            to verify snapshot exists
          - Validates path matches share container path
        """
        LOG.info("=== test_manage_nfs_snapshot_after_unmanage ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        # Create snapshot
        snapshot = self.create_snapshot(share_id)
        provider_location = snapshot['provider_location']
        original_size = int(snapshot['size'])
        LOG.info("Snapshot %s created with provider_location=%s",
                 snapshot['id'], provider_location)

        # Unmanage snapshot
        self.unmanage_snapshot(snapshot['id'])

        # Manage snapshot back
        managed = self.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['provider_location'], provider_location)
        self.assertEqual(int(managed['size']), original_size)
        self.assertEqual(managed['share_id'], share_id)
        LOG.info("NFS snapshot managed back: id=%s, "
                 "provider_location=%s", managed['id'], provider_location)

    # ----------------------------------------------------------------
    # Test: Manage NFS snapshot with explicit size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-7890-ef01-234567890102')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_nfs_snapshot_with_explicit_size(self):
        """Manage snapshot with explicit size in driver_options.

        When driver_options contains {"size": N}, the managed
        snapshot should use that size instead of defaulting to
        the share's size.
        """
        LOG.info("=== test_manage_nfs_snapshot_with_explicit_size ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=2,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        provider_location = snapshot['provider_location']

        # Unmanage
        self.unmanage_snapshot(snapshot['id'])

        # Manage back with explicit size=3
        managed = self.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
            driver_options={"size": "3"},
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(int(managed['size']), 3)
        self.assertEqual(managed['provider_location'], provider_location)
        LOG.info("NFS snapshot managed with explicit size=3: id=%s",
                 managed['id'])

    # ----------------------------------------------------------------
    # Test: Manage NFS snapshot without size defaults to share size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-7890-ef01-234567890103')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_nfs_snapshot_default_size(self):
        """Manage snapshot without size — defaults to share's size.

        When no size is specified in driver_options, the driver
        uses snapshot['share']['size'] as the default.
        """
        LOG.info("=== test_manage_nfs_snapshot_default_size ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=2,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        provider_location = snapshot['provider_location']

        # Unmanage
        self.unmanage_snapshot(snapshot['id'])

        # Manage back without specifying size
        managed = self.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(int(managed['size']), 2,
                         "Managed snapshot size should default to "
                         "share size (2G)")
        self.assertEqual(managed['provider_location'], provider_location)
        LOG.info("NFS snapshot managed with default size=2: id=%s",
                 managed['id'])

    # ----------------------------------------------------------------
    # Test: Full NFS manage snapshot lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-7890-ef01-234567890104')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_manage_snapshot_lifecycle(self):
        """Full lifecycle: create -> snapshot -> unmanage -> manage -> delete.

        This exercises the complete manage snapshot flow:
          1. Create share with snapshot_support
          2. Create snapshot
          3. Unmanage snapshot
          4. Manage snapshot back
          5. Delete managed snapshot
          6. Delete share
        """
        LOG.info("=== test_nfs_manage_snapshot_lifecycle ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        # Create snapshot
        snapshot = self.create_snapshot(share_id)
        provider_location = snapshot['provider_location']
        snapshot_id = snapshot['id']

        # Unmanage
        self.unmanage_snapshot(snapshot_id)

        # Manage back
        managed = self.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
        )
        self.assertEqual(managed['status'], 'available')
        managed_id = managed['id']

        # Delete managed snapshot
        self.shares_v2_client.delete_snapshot(managed_id)
        self._wait_for_snapshot_deletion(managed_id)

        # Delete share
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full NFS manage snapshot lifecycle completed")

    # ----------------------------------------------------------------
    # Test: Managed NFS snapshot preserves provider_location
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-7890-ef01-234567890105')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_managed_nfs_snapshot_preserves_provider_location(self):
        """Verify managed snapshot retains the same provider_location.

        The provider_location is the PowerScale snapshot ID and should
        be identical before unmanage and after manage.
        """
        LOG.info("=== test_managed_nfs_snapshot_preserves_provider_location ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        original_provider_location = snapshot['provider_location']
        self.assertIsNotNone(original_provider_location,
                             "Snapshot must have a provider_location")

        # Unmanage and re-manage
        self.unmanage_snapshot(snapshot['id'])
        managed = self.manage_snapshot(
            share_id=share_id,
            provider_location=original_provider_location,
        )

        self.assertEqual(managed['provider_location'],
                         original_provider_location,
                         "Provider location must be preserved after manage")
        LOG.info("Provider location preserved: %s",
                 original_provider_location)

    # ----------------------------------------------------------------
    # Test: Manage NFS snapshot — unmanage and re-manage twice
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-7890-ef01-234567890106')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_nfs_snapshot_twice(self):
        """Unmanage and re-manage a snapshot twice.

        Verifies that the manage/unmanage cycle is repeatable
        and the snapshot can be managed multiple times.
        """
        LOG.info("=== test_manage_nfs_snapshot_twice ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        provider_location = snapshot['provider_location']

        for cycle in range(1, 3):
            LOG.info("Manage/unmanage cycle %d", cycle)
            self.unmanage_snapshot(snapshot['id'])
            managed = self.manage_snapshot(
                share_id=share_id,
                provider_location=provider_location,
            )
            self.assertEqual(managed['status'], 'available')
            self.assertEqual(managed['provider_location'],
                             provider_location)
            snapshot = managed

        LOG.info("Two manage/unmanage cycles completed successfully")

    # ----------------------------------------------------------------
    # Negative: Manage NFS snapshot with invalid provider_location
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-7890-ef01-234567890107')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nfs_snapshot_invalid_provider_location(self):
        """Manage snapshot with non-existent provider_location fails.

        The driver calls get_snapshot_id() which returns None for
        a non-existent ID, raising ManageInvalidShareSnapshot.
        The Manila API returns manage_error status.
        """
        LOG.info("=== test_manage_nfs_snapshot_invalid_provider_location ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        # Try to manage with a bogus provider_location
        fake_provider_location = "9999999"
        name = data_utils.rand_name('ps-bad-manage-snap')
        snap = self.shares_v2_client.manage_snapshot(
            share_id=share_id,
            provider_location=fake_provider_location,
            name=name,
        )
        sn = snap.get('snapshot', snap)
        managed_id = sn['id']
        self.addCleanup(self._cleanup_manage_error_snapshot, managed_id)

        # Wait for the snapshot to enter manage_error state
        self._wait_for_snapshot_manage_error(managed_id)

        snap_detail = self.shares_v2_client.get_snapshot(managed_id)
        sn_detail = snap_detail.get('snapshot', snap_detail)
        self.assertEqual(sn_detail['status'], 'manage_error',
                         f"Snapshot should be in manage_error state, "
                         f"got {sn_detail['status']}")
        LOG.info("Manage with invalid provider_location correctly "
                 "entered manage_error for snapshot %s", managed_id)


# ===================================================================
# CIFS Manage Snapshot Tests
# ===================================================================
class _CIFSManageSnapshotTests(object):
    """Mixin: CIFS manage snapshot test methods for PowerScale."""

    @classmethod
    def skip_checks(cls):
        super(_CIFSManageSnapshotTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Manage CIFS snapshot after unmanage
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f1a2b3c4-d5e6-7890-fa01-234567890201')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_cifs_snapshot_after_unmanage(self):
        """Unmanage a CIFS snapshot, then manage it back.

        Same PowerScale side-effects as NFS manage:
          - GET /platform/1/snapshot/snapshots/<provider_location>
        """
        LOG.info("=== test_manage_cifs_snapshot_after_unmanage ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        provider_location = snapshot['provider_location']
        original_size = int(snapshot['size'])

        # Unmanage
        self.unmanage_snapshot(snapshot['id'])

        # Manage back
        managed = self.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['provider_location'], provider_location)
        self.assertEqual(int(managed['size']), original_size)
        self.assertEqual(managed['share_id'], share_id)
        LOG.info("CIFS snapshot managed back: id=%s, "
                 "provider_location=%s", managed['id'], provider_location)

    # ----------------------------------------------------------------
    # Test: Manage CIFS snapshot with explicit size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f1a2b3c4-d5e6-7890-fa01-234567890202')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_cifs_snapshot_with_explicit_size(self):
        """Manage CIFS snapshot with explicit size in driver_options."""
        LOG.info("=== test_manage_cifs_snapshot_with_explicit_size ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=2,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        provider_location = snapshot['provider_location']

        self.unmanage_snapshot(snapshot['id'])

        managed = self.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
            driver_options={"size": "3"},
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(int(managed['size']), 3)
        LOG.info("CIFS snapshot managed with explicit size=3: id=%s",
                 managed['id'])

    # ----------------------------------------------------------------
    # Test: Full CIFS manage snapshot lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f1a2b3c4-d5e6-7890-fa01-234567890203')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_manage_snapshot_lifecycle(self):
        """Full lifecycle: create -> snapshot -> unmanage -> manage ->
        delete (CIFS)."""
        LOG.info("=== test_cifs_manage_snapshot_lifecycle ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        snapshot = self.create_snapshot(share_id)
        provider_location = snapshot['provider_location']

        # Unmanage
        self.unmanage_snapshot(snapshot['id'])

        # Manage back
        managed = self.manage_snapshot(
            share_id=share_id,
            provider_location=provider_location,
        )
        self.assertEqual(managed['status'], 'available')

        # Delete managed snapshot
        self.shares_v2_client.delete_snapshot(managed['id'])
        self._wait_for_snapshot_deletion(managed['id'])

        # Delete share
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full CIFS manage snapshot lifecycle completed")

    # ----------------------------------------------------------------
    # Negative: Manage CIFS snapshot with invalid provider_location
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f1a2b3c4-d5e6-7890-fa01-234567890204')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_cifs_snapshot_invalid_provider_location(self):
        """Manage CIFS snapshot with non-existent provider_location fails."""
        LOG.info("=== test_manage_cifs_snapshot_invalid_provider_location ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        fake_provider_location = "9999999"
        name = data_utils.rand_name('ps-bad-manage-snap')
        snap = self.shares_v2_client.manage_snapshot(
            share_id=share_id,
            provider_location=fake_provider_location,
            name=name,
        )
        sn = snap.get('snapshot', snap)
        managed_id = sn['id']
        self.addCleanup(self._cleanup_manage_error_snapshot, managed_id)

        self._wait_for_snapshot_manage_error(managed_id)

        snap_detail = self.shares_v2_client.get_snapshot(managed_id)
        sn_detail = snap_detail.get('snapshot', snap_detail)
        self.assertEqual(sn_detail['status'], 'manage_error')
        LOG.info("CIFS manage with invalid provider_location correctly "
                 "entered manage_error for snapshot %s", managed_id)


# ---------------------------------------------------------------------------
# Concrete test classes wired to a Tempest-compatible base class.
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleManageSnapshotNFS(
            _NFSManageSnapshotTests,
            PowerScaleManageSnapshotTest,
            manila_base.BaseSharesAdminTest):
        """NFS manage snapshot functional tests
        (manila_tempest_tests base)."""

    class TestPowerScaleManageSnapshotCIFS(
            _CIFSManageSnapshotTests,
            PowerScaleManageSnapshotTest,
            manila_base.BaseSharesAdminTest):
        """CIFS manage snapshot functional tests
        (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleManageSnapshotNFS(
            _NFSManageSnapshotTests,
            PowerScaleManageSnapshotTest,
            tempest_test.BaseTestCase):
        """NFS manage snapshot functional tests
        (tempest.test fallback base)."""
        credentials = ['primary', 'admin']

    class TestPowerScaleManageSnapshotCIFS(
            _CIFSManageSnapshotTests,
            PowerScaleManageSnapshotTest,
            tempest_test.BaseTestCase):
        """CIFS manage snapshot functional tests
        (tempest.test fallback base)."""
        credentials = ['primary', 'admin']
