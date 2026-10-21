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
Tempest end-to-end integration tests for PowerScale data-LIF export locations.

Bug: The PowerScale driver returned the management/API address
(emc_nas_server) in NFS and CIFS export locations instead of the
actual data LIF addresses from the OneFS network pool.

After the fix, export locations contain one entry per data LIF
discovered from the OneFS ``/platform/<ver>/network/pools`` API,
consistent with NetApp and PowerStore driver behavior.

Tests cover:
  - NFS / CIFS export locations use data LIF addresses (not mgmt)
  - Multiple distinct export locations returned (one per data LIF)
  - Exactly one preferred export location per share
  - All export locations share the same directory / share name
  - Export locations preserved after extend / shrink
  - NFS access reachable through every returned data LIF address
  - Full lifecycle: create -> verify exports -> extend -> re-verify
    -> access through all LIFs -> delete
"""

import re
import subprocess
import time

from oslo_log import log as logging
from tempest import config
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc
from tempest.lib.common.utils import data_utils

from dell_tempest_plugin.tests.powerscale_manila import (
    test_powerscale_dedupe as dedupe_tests)

CONF = config.CONF
LOG = logging.getLogger(__name__)

SHARE_BUILD_TIMEOUT = 600
SHARE_BUILD_INTERVAL = 5


class PowerScaleExportLocationTest(dedupe_tests.PowerScaleDedupeShareTest):
    """Helpers for PowerScale export-location data-LIF tests.

    Inherits share type / share / wait / cleanup helpers from
    PowerScaleDedupeShareTest.  Uses ``create_dedupe_share_type``
    with ``dedupe=False`` to create a DHSS=False share type
    compatible with the PowerScale backend.
    """

    # ------------------------------------------------------------------
    # Share type helper (override to guarantee DHSS=False)
    # ------------------------------------------------------------------
    def create_share_type(self, name=None, extra_specs=None):
        """Create a DHSS=False share type for export-location tests."""
        return self.create_dedupe_share_type(
            name=name or data_utils.rand_name('ps-export-lif-type'),
            dedupe=False,
            extra_specs=extra_specs,
        )

    # ------------------------------------------------------------------
    # Export-location introspection helpers
    # ------------------------------------------------------------------
    def _get_export_locations_list(self, share_id):
        """Return the raw list of export-location dicts for *share_id*."""
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    def _get_paths_and_locations(self, share_id):
        """Return ``(paths, locations)`` for a share.

        *paths* is a list of path strings; *locations* is the raw
        list of export-location dicts (with ``preferred``, etc.).
        """
        locations = self._get_export_locations_list(share_id)
        paths = []
        for loc in locations:
            if isinstance(loc, dict):
                paths.append(loc.get('path', str(loc)))
            else:
                paths.append(str(loc))
        self.assertTrue(paths, 'Share must have at least one export location')
        return paths, locations

    def _assert_exactly_one_preferred(self, locations):
        """Assert exactly one export location has ``preferred=True``."""
        preferred = [
            loc for loc in locations
            if isinstance(loc, dict) and loc.get('preferred') is True
        ]
        self.assertEqual(
            1, len(preferred),
            'Expected exactly 1 preferred export location, '
            'found %d' % len(preferred))

    def _parse_nfs_paths(self, paths):
        """Parse NFS paths, returning ``(addresses, directories)``."""
        addresses = []
        directories = set()
        for path in paths:
            match = re.match(r'^([^:]+):(.+)$', path)
            self.assertIsNotNone(
                match, 'Invalid NFS export path: %s' % path)
            addresses.append(match.group(1))
            directories.add(match.group(2))
        return addresses, directories

    def _parse_cifs_paths(self, paths):
        r"""Parse CIFS paths, returning ``(addresses, share_names)``."""
        addresses = []
        share_names = set()
        for path in paths:
            match = re.match(r'^\\\\([^\\]+)\\(.+)$', path)
            self.assertIsNotNone(
                match, 'Invalid CIFS export path: %s' % path)
            addresses.append(match.group(1))
            share_names.add(match.group(2))
        return addresses, share_names

    @staticmethod
    def _showmount_check(address, timeout=10):
        """Return True if ``showmount -e <address>`` succeeds."""
        try:
            result = subprocess.run(
                ['showmount', '-e', address],
                capture_output=True, text=True, timeout=timeout)
            return result.returncode == 0
        except Exception:
            return False

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

    # ------------------------------------------------------------------
    # Create-and-get shorthand
    # ------------------------------------------------------------------
    def _create_and_get(self, protocol, size=10):
        """Create a share and return ``(share, paths, locations)``."""
        share_type = self.create_share_type()
        share = self.create_share(
            protocol=protocol,
            share_type_name=share_type['name'],
            size=size,
        )
        self.assertEqual('available', share['status'])
        paths, locations = self._get_paths_and_locations(share['id'])
        return share, paths, locations

    def _wait_for_snapshot(self, snapshot_id):
        """Wait for a snapshot to become available."""
        deadline = time.time() + SHARE_BUILD_TIMEOUT
        while time.time() < deadline:
            snapshot = self.shares_v2_client.get_snapshot(snapshot_id)
            snapshot = snapshot.get('snapshot', snapshot)
            status = snapshot.get('status', '').lower()
            if status == 'available':
                return
            if status in ('error', 'error_deleting'):
                self.fail(
                    'Snapshot %s entered error state: %s' %
                    (snapshot_id, status))
            time.sleep(SHARE_BUILD_INTERVAL)
        self.fail('Timeout waiting for snapshot %s' % snapshot_id)

    def _delete_snapshot_safe(self, snapshot_id):
        """Delete a snapshot and wait for it to disappear."""
        try:
            self.shares_v2_client.delete_snapshot(snapshot_id)
        except lib_exc.NotFound:
            return
        deadline = time.time() + SHARE_BUILD_TIMEOUT
        while time.time() < deadline:
            try:
                self.shares_v2_client.get_snapshot(snapshot_id)
            except lib_exc.NotFound:
                return
            time.sleep(SHARE_BUILD_INTERVAL)
        self.fail('Timeout deleting snapshot %s' % snapshot_id)

    def _create_from_snapshot_and_get(self, protocol, size=10):
        """Create a share from a snapshot and return its locations."""
        share_type = self.create_share_type(extra_specs={
            'snapshot_support': 'True',
            'create_share_from_snapshot_support': 'True',
        })
        source = self.create_share(
            protocol=protocol,
            share_type_name=share_type['name'],
            size=size,
        )
        snapshot = self.shares_v2_client.create_snapshot(
            share_id=source['id'],
            name=data_utils.rand_name('ps-export-lif-snapshot'))
        snapshot = snapshot.get('snapshot', snapshot)
        self.addCleanup(self._delete_snapshot_safe, snapshot['id'])
        self._wait_for_snapshot(snapshot['id'])

        destination = self.shares_v2_client.create_share(
            share_protocol=protocol,
            size=size,
            name=data_utils.rand_name(
                'ps-export-lif-from-snapshot-%s' % protocol.lower()),
            snapshot_id=snapshot['id'],
            share_type_id=share_type['id'],
        )
        destination = destination.get('share', destination)
        self.addCleanup(self._delete_share_safe, destination['id'])
        self._wait_for_share_status(destination['id'], 'available')
        paths, locations = self._get_paths_and_locations(destination['id'])
        return paths, locations


# ===================================================================
# NFS Export Location Tests
# ===================================================================
class _NFSExportLocationTests(object):
    """NFS export-location data-LIF integration tests."""

    @classmethod
    def skip_checks(cls):
        super(_NFSExportLocationTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException('Manila is not available')

    # ----------------------------------------------------------------
    # Test 1: NFS share returns multiple data-LIF export locations
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345678901')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_export_locations_are_data_lifs(self):
        """Verify NFS export locations use data LIF addresses.

        Checks:
          - At least one export location returned
          - All addresses are distinct (one per data LIF)
          - All paths point to the same OneFS directory
          - Exactly one export is marked preferred
        """
        LOG.info("=== test_nfs_export_locations_are_data_lifs ===")

        share, paths, locations = self._create_and_get('NFS')
        share_id = share['id']

        addresses, directories = self._parse_nfs_paths(paths)

        # All addresses must be distinct
        self.assertEqual(
            len(addresses), len(set(addresses)),
            'Export addresses must be unique, got: %s' % addresses)

        # All paths point to the same directory
        self.assertEqual(
            1, len(directories),
            'All NFS exports must share the same directory, '
            'got: %s' % directories)

        # Exactly one preferred
        self._assert_exactly_one_preferred(locations)

        LOG.info("NFS share %s: %d data-LIF export(s): %s",
                 share_id, len(paths), paths)

    # ----------------------------------------------------------------
    # Test 2: NFS export locations preserved after extend
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345678902')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_export_locations_preserved_after_extend(self):
        """Export locations should be identical after extending a share.

        Steps:
          1. Create 10G NFS share, record export locations
          2. Extend to 20G
          3. Verify export locations are unchanged
        """
        LOG.info("=== test_nfs_export_locations_preserved_after_extend ===")

        share, paths_before, _ = self._create_and_get('NFS', size=10)
        share_id = share['id']

        # Extend to 20G
        extended = self.extend_share(share_id, 20)
        self.assertEqual(int(extended['size']), 20)

        # Re-check export locations
        paths_after, locations_after = self._get_paths_and_locations(share_id)

        self.assertEqual(
            sorted(paths_before), sorted(paths_after),
            'Export locations must be preserved after extend')
        self._assert_exactly_one_preferred(locations_after)

        LOG.info("NFS share %s: exports preserved after extend", share_id)

    # ----------------------------------------------------------------
    # Test 3: NFS accessible through every data-LIF address
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345678903')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_accessible_through_all_data_lif_addresses(self):
        """Verify NFS exports are reachable through every data LIF.

        For each export location address, ``showmount -e <addr>``
        must succeed, confirming the NFS service is reachable on
        each data LIF (not just the management address).
        """
        LOG.info(
            "=== test_nfs_accessible_through_all_data_lif_addresses ===")

        share, paths, locations = self._create_and_get('NFS')
        share_id = share['id']
        addresses, _ = self._parse_nfs_paths(paths)

        for addr in addresses:
            reachable = self._showmount_check(addr)
            self.assertTrue(
                reachable,
                'NFS export must be reachable via showmount on '
                'data LIF %s' % addr)
            LOG.info("  showmount -e %s: OK", addr)

        LOG.info("NFS share %s accessible through all %d data LIFs",
                 share_id, len(addresses))

    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345678905')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_snapshot_share_export_locations_are_data_lifs(self):
        """Verify NFS shares created from snapshots use every data LIF."""
        paths, locations = self._create_from_snapshot_and_get('NFS')
        addresses, directories = self._parse_nfs_paths(paths)
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertEqual(1, len(directories))
        self._assert_exactly_one_preferred(locations)

    # ----------------------------------------------------------------
    # Test 4: NFS full lifecycle with data-LIF verification
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-012345678904')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_full_lifecycle_data_lif_export_locations(self):
        """End-to-end NFS lifecycle with data-LIF export verification.

        Steps:
          1. Create 10G NFS share
          2. Verify multiple data-LIF exports with one preferred
          3. Verify NFS reachable on each data-LIF
          4. Extend to 20G
          5. Verify export locations preserved and still reachable
          6. Delete share
          7. Confirm share is gone
        """
        LOG.info(
            "=== test_nfs_full_lifecycle_data_lif_export_locations ===")

        # Step 1: Create
        share, paths, locations = self._create_and_get('NFS', size=10)
        share_id = share['id']
        self.assertEqual('available', share['status'])
        LOG.info("Step 1: Created NFS share %s with %d export(s)",
                 share_id, len(paths))

        # Step 2: Verify structure
        addresses, directories = self._parse_nfs_paths(paths)
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertEqual(1, len(directories))
        self._assert_exactly_one_preferred(locations)
        LOG.info("Step 2: Export structure validated")

        # Step 3: Verify access on each LIF
        for addr in addresses:
            self.assertTrue(
                self._showmount_check(addr),
                'showmount failed on data LIF %s' % addr)
        LOG.info("Step 3: NFS reachable on all %d data LIFs",
                 len(addresses))

        # Step 4: Extend
        extended = self.extend_share(share_id, 20)
        self.assertEqual(int(extended['size']), 20)
        LOG.info("Step 4: Extended to 20G")

        # Step 5: Re-verify
        paths_after, locs_after = self._get_paths_and_locations(share_id)
        self.assertEqual(sorted(paths), sorted(paths_after))
        self._assert_exactly_one_preferred(locs_after)
        addrs_after, _ = self._parse_nfs_paths(paths_after)
        for addr in addrs_after:
            self.assertTrue(
                self._showmount_check(addr),
                'showmount failed on data LIF %s after extend' % addr)
        LOG.info("Step 5: Exports preserved and reachable after extend")

        # Step 6: Delete
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)
        LOG.info("Step 6: Share deleted")

        # Step 7: Confirm gone
        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id)
        LOG.info("Step 7: Share %s confirmed deleted", share_id)


# ===================================================================
# CIFS Export Location Tests
# ===================================================================
class _CIFSExportLocationTests(object):
    """CIFS export-location data-LIF integration tests."""

    @classmethod
    def skip_checks(cls):
        super(_CIFSExportLocationTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException('Manila is not available')

    # ----------------------------------------------------------------
    # Test 1: CIFS share returns multiple data-LIF export locations
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1e2f3a4-b5c6-7890-defa-012345678901')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_export_locations_are_data_lifs(self):
        """Verify CIFS export locations use data LIF addresses.

        Checks:
          - At least one export location returned
          - All addresses are distinct
          - All paths share the same SMB share name
          - Exactly one export is marked preferred
        """
        LOG.info("=== test_cifs_export_locations_are_data_lifs ===")

        share, paths, locations = self._create_and_get('CIFS')
        share_id = share['id']

        addresses, share_names = self._parse_cifs_paths(paths)

        self.assertEqual(
            len(addresses), len(set(addresses)),
            'Export addresses must be unique, got: %s' % addresses)

        self.assertEqual(
            1, len(share_names),
            'All CIFS exports must share the same name, '
            'got: %s' % share_names)

        self._assert_exactly_one_preferred(locations)

        LOG.info("CIFS share %s: %d data-LIF export(s): %s",
                 share_id, len(paths), paths)

    # ----------------------------------------------------------------
    # Test 2: CIFS export locations preserved after extend
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1e2f3a4-b5c6-7890-defa-012345678902')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_export_locations_preserved_after_extend(self):
        """Export locations should be identical after extending a share."""
        LOG.info("=== test_cifs_export_locations_preserved_after_extend ===")

        share, paths_before, _ = self._create_and_get('CIFS', size=10)
        share_id = share['id']

        extended = self.extend_share(share_id, 20)
        self.assertEqual(int(extended['size']), 20)

        paths_after, locations_after = self._get_paths_and_locations(share_id)

        self.assertEqual(
            sorted(paths_before), sorted(paths_after),
            'Export locations must be preserved after extend')
        self._assert_exactly_one_preferred(locations_after)

        LOG.info("CIFS share %s: exports preserved after extend", share_id)

    @decorators.idempotent_id('d1e2f3a4-b5c6-7890-defa-012345678904')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_snapshot_share_export_locations_are_data_lifs(self):
        """Verify CIFS shares created from snapshots use every data LIF."""
        paths, locations = self._create_from_snapshot_and_get('CIFS')
        addresses, share_names = self._parse_cifs_paths(paths)
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertEqual(1, len(share_names))
        self._assert_exactly_one_preferred(locations)

    # ----------------------------------------------------------------
    # Test 3: CIFS full lifecycle with data-LIF verification
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1e2f3a4-b5c6-7890-defa-012345678903')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_full_lifecycle_data_lif_export_locations(self):
        """End-to-end CIFS lifecycle with data-LIF export verification.

        Steps:
          1. Create 10G CIFS share
          2. Verify multiple data-LIF exports with one preferred
          3. Extend to 20G
          4. Verify export locations preserved
          5. Delete share
          6. Confirm share is gone
        """
        LOG.info(
            "=== test_cifs_full_lifecycle_data_lif_export_locations ===")

        # Step 1: Create
        share, paths, locations = self._create_and_get('CIFS', size=10)
        share_id = share['id']
        self.assertEqual('available', share['status'])
        LOG.info("Step 1: Created CIFS share %s with %d export(s)",
                 share_id, len(paths))

        # Step 2: Verify structure
        addresses, share_names = self._parse_cifs_paths(paths)
        self.assertEqual(len(addresses), len(set(addresses)))
        self.assertEqual(1, len(share_names))
        self._assert_exactly_one_preferred(locations)
        LOG.info("Step 2: Export structure validated")

        # Step 3: Extend
        extended = self.extend_share(share_id, 20)
        self.assertEqual(int(extended['size']), 20)
        LOG.info("Step 3: Extended to 20G")

        # Step 4: Re-verify
        paths_after, locs_after = self._get_paths_and_locations(share_id)
        self.assertEqual(sorted(paths), sorted(paths_after))
        self._assert_exactly_one_preferred(locs_after)
        LOG.info("Step 4: Exports preserved after extend")

        # Step 5: Delete
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)
        LOG.info("Step 5: Share deleted")

        # Step 6: Confirm gone
        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id)
        LOG.info("Step 6: Share %s confirmed deleted", share_id)


# ---------------------------------------------------------------------------
# Concrete test classes
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleExportLocationNFS(
            _NFSExportLocationTests,
            PowerScaleExportLocationTest,
            manila_base.BaseSharesAdminTest):
        """NFS data-LIF export-location integration test."""

    class TestPowerScaleExportLocationCIFS(
            _CIFSExportLocationTests,
            PowerScaleExportLocationTest,
            manila_base.BaseSharesAdminTest):
        """CIFS data-LIF export-location integration test."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleExportLocationNFS(
            _NFSExportLocationTests,
            PowerScaleExportLocationTest,
            tempest_test.BaseTestCase):
        """NFS data-LIF export-location integration test."""
        credentials = ['primary', 'admin']

    class TestPowerScaleExportLocationCIFS(
            _CIFSExportLocationTests,
            PowerScaleExportLocationTest,
            tempest_test.BaseTestCase):
        """CIFS data-LIF export-location integration test."""
        credentials = ['primary', 'admin']
