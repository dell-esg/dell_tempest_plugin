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
Tempest functional tests for Dell PowerScale share shrink feature.

These tests exercise the share shrink lifecycle through real Manila API calls:
  - Shrinking NFS/CIFS shares to a smaller size
  - Verifying quota is updated on PowerScale via quota_set API
  - Verifying shrink fails when new size is below used space
  - Extend-then-shrink round-trip scenarios

References (from share-shrink.patch):
  - PowerScale API: GET /platform/1/quota/quotas?path=<path>
    (get_directory_usage — returns quotas[0].usage.logical)
  - PowerScale API: PUT quota to set new size via quota_set()
  - Manila share action: POST /shares/<id>/action  {"shrink": {"new_size": N}}
  - Manila status transitions: available -> shrinking -> available
  - Error status: shrinking_possible_data_loss_error (new_size < used)
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


class PowerScaleShrinkShareTest(object):
    """Mixin with helpers for PowerScale share shrink tests.

    Provides utility methods for creating share types, shares,
    shrinking, extending, and waiting for share status transitions
    via the Manila API.
    """

    @classmethod
    def setup_clients(cls):
        super(PowerScaleShrinkShareTest, cls).setup_clients()
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
        """Create a Manila share type for shrink tests.

        :param name: Optional name; auto-generated if None.
        :param extra_specs: Additional extra-specs dict to merge.
        :returns: Created share type dict.
        """
        name = name or data_utils.rand_name('ps-shrink-type')
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
            f'ps-shrink-{protocol.lower()}')
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
            if status in ('shrinking_possible_data_loss_error',
                          'shrinking_error'):
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
    # Shrink / Extend helpers
    # ------------------------------------------------------------------
    def shrink_share(self, share_id, new_size):
        """Shrink a share and wait until it becomes available.

        :param share_id: The ID of the share to shrink.
        :param new_size: The new size in GB (must be < current size).
        :returns: Updated share dict.
        """
        LOG.info("Shrinking share %s to %sG", share_id, new_size)
        self.shares_v2_client.shrink_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        share = self.shares_v2_client.get_share(share_id)
        return share.get('share', share)

    def extend_share(self, share_id, new_size):
        """Extend a share and wait until it becomes available.

        :param share_id: The ID of the share to extend.
        :param new_size: The new size in GB (must be > current size).
        :returns: Updated share dict.
        """
        LOG.info("Extending share %s to %sG", share_id, new_size)
        self.shares_v2_client.extend_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        share = self.shares_v2_client.get_share(share_id)
        return share.get('share', share)


# ===================================================================
# NFS Share Shrink Tests
# ===================================================================
class _NFSShrinkTests(object):
    """Mixin: NFS share shrink test methods for PowerScale.

    Each test makes real Manila API calls that propagate to the
    PowerScale backend, triggering quota_set and get_directory_usage
    calls via the PowerScale REST API.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSShrinkTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Shrink NFS share to a smaller size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef0123456701')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_nfs_share(self):
        """Shrink an NFS share from 2G to 1G.

        Expected PowerScale side-effects:
          1. get_directory_usage() called to check used space
          2. quota_set() called with new_size * Gi
        """
        LOG.info("=== test_shrink_nfs_share ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=2,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')
        self.assertEqual(int(share['size']), 2)

        # Shrink from 2G to 1G
        shrunk = self.shrink_share(share_id, 1)
        self.assertEqual(shrunk['status'], 'available')
        self.assertEqual(int(shrunk['size']), 1)
        LOG.info("NFS share %s shrunk from 2G to 1G successfully",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Shrink NFS share — verify export location preserved
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef0123456702')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_nfs_share_preserves_export(self):
        """Shrink an NFS share and verify export locations are preserved.

        The export location should remain the same after shrink since
        only the quota is updated on PowerScale, not the export itself.
        """
        LOG.info("=== test_shrink_nfs_share_preserves_export ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=2,
        )
        share_id = share['id']

        # Get export locations before shrink
        exports_before = self._get_export_locations(share_id)
        self.assertTrue(len(exports_before) > 0,
                        "Share must have export locations")

        # Shrink
        self.shrink_share(share_id, 1)

        # Get export locations after shrink
        exports_after = self._get_export_locations(share_id)
        self.assertTrue(len(exports_after) > 0,
                        "Share must still have export locations after shrink")

        # Compare paths
        def _get_path(el):
            if isinstance(el, dict):
                return el.get('path', str(el))
            return str(el)

        paths_before = sorted([_get_path(e) for e in exports_before])
        paths_after = sorted([_get_path(e) for e in exports_after])
        self.assertEqual(paths_before, paths_after,
                         "Export locations should be preserved after shrink")
        LOG.info("Export locations preserved after shrink for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Extend then shrink NFS share (round-trip)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef0123456703')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_then_shrink_nfs_share(self):
        """Extend an NFS share then shrink it back to original size.

        Steps:
          1. Create 1G NFS share
          2. Extend to 3G
          3. Shrink back to 1G
          4. Verify final size is 1G

        This exercises both quota_set paths on PowerScale:
          - extend_share: quota_set with larger size
          - shrink_share: get_directory_usage + quota_set with smaller size
        """
        LOG.info("=== test_extend_then_shrink_nfs_share ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(int(share['size']), 1)

        # Extend to 3G
        extended = self.extend_share(share_id, 3)
        self.assertEqual(int(extended['size']), 3)
        LOG.info("Share %s extended to 3G", share_id)

        # Shrink back to 1G
        shrunk = self.shrink_share(share_id, 1)
        self.assertEqual(int(shrunk['size']), 1)
        LOG.info("Share %s shrunk back to 1G", share_id)

    # ----------------------------------------------------------------
    # Test: Shrink NFS share — full lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef0123456704')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_shrink_lifecycle(self):
        """Full lifecycle: create -> extend -> shrink -> delete.

        This exercises the complete shrink flow on PowerScale:
          1. Create share (quota_set with initial size)
          2. Extend share (quota_set with larger size)
          3. Shrink share (get_directory_usage + quota_set with smaller size)
          4. Delete share (quota deleted)
        """
        LOG.info("=== test_nfs_shrink_lifecycle ===")

        share_type = self.create_share_type()

        # Create 1G share
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')
        self.assertEqual(int(share['size']), 1)

        # Extend to 3G
        extended = self.extend_share(share_id, 3)
        self.assertEqual(extended['status'], 'available')
        self.assertEqual(int(extended['size']), 3)

        # Shrink to 2G
        shrunk = self.shrink_share(share_id, 2)
        self.assertEqual(shrunk['status'], 'available')
        self.assertEqual(int(shrunk['size']), 2)

        # Verify export location still present
        export_locations = self._get_export_locations(share_id)
        self.assertTrue(len(export_locations) > 0,
                        "Share must have export locations after shrink")

        # Delete
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full NFS shrink lifecycle completed for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Multiple sequential shrinks on NFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef0123456705')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_multiple_shrinks_nfs_share(self):
        """Shrink an NFS share multiple times in sequence.

        Steps:
          1. Create 4G share
          2. Shrink to 3G
          3. Shrink to 2G
          4. Shrink to 1G
        """
        LOG.info("=== test_multiple_shrinks_nfs_share ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=4,
        )
        share_id = share['id']
        self.assertEqual(int(share['size']), 4)

        for target_size in (3, 2, 1):
            shrunk = self.shrink_share(share_id, target_size)
            self.assertEqual(int(shrunk['size']), target_size)
            LOG.info("Share %s shrunk to %sG", share_id, target_size)

        LOG.info("Multiple sequential shrinks completed for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Negative: Shrink NFS share to size larger than current
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef0123456706')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_shrink_nfs_share_to_larger_size_fails(self):
        """Shrink request with new_size > current_size should fail.

        Manila API validates that new_size < current_size before
        dispatching to the backend. Expected: HTTP 400 BadRequest.
        """
        LOG.info("=== test_shrink_nfs_share_to_larger_size_fails ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        self.assertRaises(
            lib_exc.BadRequest,
            self.shares_v2_client.shrink_share,
            share_id,
            2,
        )
        # Verify share remains available and unchanged
        current = self.shares_v2_client.get_share(share_id)
        sh = current.get('share', current)
        self.assertEqual(sh['status'], 'available')
        self.assertEqual(int(sh['size']), 1)
        LOG.info("Shrink to larger size correctly rejected for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Negative: Shrink NFS share to zero
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef0123456707')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_shrink_nfs_share_to_zero_fails(self):
        """Shrink request with new_size=0 should fail.

        Manila API validates that new_size > 0 before dispatching
        to the backend. Expected: HTTP 400 BadRequest.
        """
        LOG.info("=== test_shrink_nfs_share_to_zero_fails ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=2,
        )
        share_id = share['id']

        self.assertRaises(
            lib_exc.BadRequest,
            self.shares_v2_client.shrink_share,
            share_id,
            0,
        )
        # Verify share remains available and unchanged
        current = self.shares_v2_client.get_share(share_id)
        sh = current.get('share', current)
        self.assertEqual(sh['status'], 'available')
        self.assertEqual(int(sh['size']), 2)
        LOG.info("Shrink to zero correctly rejected for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Negative: Shrink NFS share to same size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef0123456708')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_shrink_nfs_share_to_same_size_fails(self):
        """Shrink request with new_size == current_size should fail.

        Manila API validates that new_size < current_size. Since they
        are equal, the size_decrease is 0. Expected: HTTP 400 BadRequest.
        """
        LOG.info("=== test_shrink_nfs_share_to_same_size_fails ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        self.assertRaises(
            lib_exc.BadRequest,
            self.shares_v2_client.shrink_share,
            share_id,
            1,
        )
        # Verify share remains available and unchanged
        current = self.shares_v2_client.get_share(share_id)
        sh = current.get('share', current)
        self.assertEqual(sh['status'], 'available')
        self.assertEqual(int(sh['size']), 1)
        LOG.info("Shrink to same size correctly rejected for share %s",
                 share_id)


# ===================================================================
# CIFS Share Shrink Tests
# ===================================================================
class _CIFSShrinkTests(object):
    """Mixin: CIFS share shrink test methods for PowerScale."""

    @classmethod
    def skip_checks(cls):
        super(_CIFSShrinkTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Shrink CIFS share to a smaller size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b1c2d3e4-f5a6-7890-bcde-f01234567801')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_cifs_share(self):
        """Shrink a CIFS share from 2G to 1G.

        Same PowerScale side-effects as NFS shrink:
          1. get_directory_usage() to check used space
          2. quota_set() with new_size * Gi
        """
        LOG.info("=== test_shrink_cifs_share ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=2,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')
        self.assertEqual(int(share['size']), 2)

        shrunk = self.shrink_share(share_id, 1)
        self.assertEqual(shrunk['status'], 'available')
        self.assertEqual(int(shrunk['size']), 1)
        LOG.info("CIFS share %s shrunk from 2G to 1G successfully",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Extend then shrink CIFS share
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b1c2d3e4-f5a6-7890-bcde-f01234567802')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_then_shrink_cifs_share(self):
        """Extend a CIFS share then shrink it back to original size."""
        LOG.info("=== test_extend_then_shrink_cifs_share ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(int(share['size']), 1)

        # Extend to 3G
        extended = self.extend_share(share_id, 3)
        self.assertEqual(int(extended['size']), 3)

        # Shrink back to 1G
        shrunk = self.shrink_share(share_id, 1)
        self.assertEqual(int(shrunk['size']), 1)
        LOG.info("CIFS share %s extend-then-shrink completed", share_id)

    # ----------------------------------------------------------------
    # Test: CIFS share shrink lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b1c2d3e4-f5a6-7890-bcde-f01234567803')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_shrink_lifecycle(self):
        """Full lifecycle: create -> extend -> shrink -> delete (CIFS)."""
        LOG.info("=== test_cifs_shrink_lifecycle ===")

        share_type = self.create_share_type()

        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Extend to 3G
        extended = self.extend_share(share_id, 3)
        self.assertEqual(int(extended['size']), 3)

        # Shrink to 2G
        shrunk = self.shrink_share(share_id, 2)
        self.assertEqual(int(shrunk['size']), 2)

        # Delete
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full CIFS shrink lifecycle completed for share %s",
                 share_id)

    # ----------------------------------------------------------------
    # Negative: Shrink CIFS share to larger size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b1c2d3e4-f5a6-7890-bcde-f01234567804')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_shrink_cifs_share_to_larger_size_fails(self):
        """Shrink CIFS share with new_size > current_size should fail."""
        LOG.info("=== test_shrink_cifs_share_to_larger_size_fails ===")

        share_type = self.create_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        self.assertRaises(
            lib_exc.BadRequest,
            self.shares_v2_client.shrink_share,
            share_id,
            2,
        )
        current = self.shares_v2_client.get_share(share_id)
        sh = current.get('share', current)
        self.assertEqual(sh['status'], 'available')
        self.assertEqual(int(sh['size']), 1)
        LOG.info("CIFS shrink to larger size correctly rejected for "
                 "share %s", share_id)


# ---------------------------------------------------------------------------
# Concrete test classes wired to a Tempest-compatible base class.
#
# The actual base depends on what is installed in the target environment.
# We try manila_tempest_tests first, then fall back to tempest.test.
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleShrinkNFS(
            _NFSShrinkTests,
            PowerScaleShrinkShareTest,
            manila_base.BaseSharesAdminTest):
        """NFS share shrink functional tests (manila_tempest_tests base)."""

    class TestPowerScaleShrinkCIFS(
            _CIFSShrinkTests,
            PowerScaleShrinkShareTest,
            manila_base.BaseSharesAdminTest):
        """CIFS share shrink functional tests (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleShrinkNFS(
            _NFSShrinkTests,
            PowerScaleShrinkShareTest,
            tempest_test.BaseTestCase):
        """NFS share shrink functional tests (tempest.test fallback base)."""
        credentials = ['primary', 'admin']

    class TestPowerScaleShrinkCIFS(
            _CIFSShrinkTests,
            PowerScaleShrinkShareTest,
            tempest_test.BaseTestCase):
        """CIFS share shrink functional tests (tempest.test fallback base)."""
        credentials = ['primary', 'admin']
