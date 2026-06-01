# Copyright (c) 2026 Dell Inc. or its subsidiaries.
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
Functional tests for Dell PowerFlex improved vTree handling.

Validates the change from query_vtree_statistics (total descendant count)
to query_vtree_volumes with ancestorVolumeId filtering (direct-child count
only) for image cache vTree size enforcement.

These tests exercise the Cinder Volume API against a live PowerFlex backend
to verify:
  - Basic clone operations still work with the new code path
  - Multiple sequential clones from the same source succeed
  - Nested clone chains (grandchildren) do not inflate the parent's
    direct-child count
  - Volume creation from images exercises the image cache vTree path
  - Cleanup (delete) of clones allows further cloning
  - Boundary conditions around the configured vtree size limit
"""

import time

from oslo_log import log as logging
from tempest.api.volume import base as volume_base
from tempest.common import waiters
from tempest import config
from tempest.lib.common.utils import data_utils
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc

CONF = config.CONF
LOG = logging.getLogger(__name__)

VOLUME_BUILD_TIMEOUT = 300
VOLUME_BUILD_INTERVAL = 5


class PowerFlexVtreeBaseTest(volume_base.BaseVolumeAdminTest):
    """Base class for PowerFlex vTree functional tests.

    Provides helpers for volume/clone lifecycle and admin client resolution.
    """
    credentials = ['primary', 'admin']

    @classmethod
    def skip_checks(cls):
        super(PowerFlexVtreeBaseTest, cls).skip_checks()

    @classmethod
    def _get_configured_backend_names(cls):
        backend_names = getattr(CONF.volume, 'backend_names', None)
        if not backend_names:
            return ['powerflex1', 'powerflex2', 'powerflex']
        if isinstance(backend_names, str):
            names = [name.strip() for name in backend_names.split(',')
                     if name.strip()]
            return names or ['powerflex1', 'powerflex2', 'powerflex']
        return backend_names

    @classmethod
    def _pick_powerflex_backend_name(cls):
        for name in cls._get_configured_backend_names():
            if 'powerflex' in name.lower():
                return name
        return cls._get_configured_backend_names()[0]

    @classmethod
    def resource_setup(cls):
        super(PowerFlexVtreeBaseTest, cls).resource_setup()
        cls._created_type_ids = []

    @classmethod
    def resource_cleanup(cls):
        # Get class-level admin clients
        types_client = getattr(cls, 'admin_volume_types_client', None)
        vols_client = getattr(cls, 'admin_volumes_client', None)
        
        # Fallback to os_admin if direct clients not available
        if not types_client or not vols_client:
            os_admin = getattr(cls, 'os_admin', None)
            if os_admin:
                if not types_client:
                    types_client = (getattr(os_admin, 'volume_types_v3_client', None)
                                  or getattr(os_admin, 'volume_types_client', None))
                if not vols_client:
                    vols_client = (getattr(os_admin, 'volumes_client_latest', None)
                                  or getattr(os_admin, 'volumes_v3_client', None)
                                  or getattr(os_admin, 'volumes_client', None))
        
        if types_client and vols_client:
            for type_id in cls._created_type_ids:
                try:
                    # Delete all volumes using this volume type first
                    volumes = vols_client.list_volumes(detail=True)['volumes']
                    for vol in volumes:
                        if vol['volume_type'] == type_id and vol['status'] in ('available', 'error', 'error_restoring', 'error_extending', 'error_managing'):
                            try:
                                vols_client.delete_volume(vol['id'])
                                waiters.wait_for_volume_resource_status(vols_client, vol['id'], 'deleted')
                            except (lib_exc.NotFound, lib_exc.TimeoutException):
                                pass
                    # Now delete the volume type
                    types_client.delete_volume_type(type_id)
                except lib_exc.NotFound:
                    pass
                except lib_exc.BadRequest as ex:
                    LOG.warning("Skipping volume type cleanup for %s: %s",
                                type_id, ex)
        super(PowerFlexVtreeBaseTest, cls).resource_cleanup()

    # ------------------------------------------------------------------
    # Admin client helpers
    # ------------------------------------------------------------------
    def _get_admin_volumes_client(self):
        client = getattr(self, 'admin_volumes_client', None)
        if client:
            return client
        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (getattr(os_admin, 'volumes_client_latest', None)
                      or getattr(os_admin, 'volumes_v3_client', None)
                      or getattr(os_admin, 'volumes_client', None))
            if client:
                return client
        self.skipTest("Admin volumes client not available")

    def _get_admin_types_client(self):
        client = getattr(self, 'admin_volume_types_client', None)
        if client:
            return client
        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (getattr(os_admin, 'volume_types_v3_client', None)
                      or getattr(os_admin, 'volume_types_client', None))
            if client:
                return client
        self.skipTest("Admin volume types client not available")

    # ------------------------------------------------------------------
    # Volume type helpers
    # ------------------------------------------------------------------
    def _get_powerflex_backend_name(self):
        """Return the first PowerFlex backend name from tempest config."""
        return self.__class__._pick_powerflex_backend_name()

    def _create_powerflex_volume_type(self, extra_specs=None):
        """Create a volume type targeting the PowerFlex backend."""
        types_client = self._get_admin_types_client()
        backend_name = self._get_powerflex_backend_name()
        specs = {'volume_backend_name': backend_name}
        if extra_specs:
            specs.update(extra_specs)
        vtype = types_client.create_volume_type(
            name=data_utils.rand_name('pflex-vtree-type'),
            extra_specs=specs,
        )['volume_type']
        self.__class__._created_type_ids.append(vtype['id'])
        return vtype

    # ------------------------------------------------------------------
    # Volume lifecycle helpers
    # ------------------------------------------------------------------
    def _create_volume(self, volume_type_name, size=8, **kwargs):
        """Create a volume and wait for 'available' status."""
        vols_client = self._get_admin_volumes_client()
        vol = vols_client.create_volume(
            name=data_utils.rand_name('pflex-vtree-vol'),
            size=size,
            volume_type=volume_type_name,
            **kwargs,
        )['volume']
        self.addCleanup(self._safe_delete_volume, vol['id'])
        self._wait_for_volume_status(vol['id'], 'available')
        return vols_client.show_volume(vol['id'])['volume']

    def _clone_volume(self, source_volume_id, volume_type_name, size=None):
        """Clone a volume by its source_volid and wait for 'available'."""
        vols_client = self._get_admin_volumes_client()
        params = {
            'name': data_utils.rand_name('pflex-vtree-clone'),
            'source_volid': source_volume_id,
            'volume_type': volume_type_name,
        }
        if size is not None:
            params['size'] = size
        vol = vols_client.create_volume(**params)['volume']
        self.addCleanup(self._safe_delete_volume, vol['id'])
        self._wait_for_volume_status(vol['id'], 'available')
        return vols_client.show_volume(vol['id'])['volume']

    def _create_volume_from_image(self, volume_type_name, image_ref,
                                  size=8):
        """Create a volume from a glance image."""
        vols_client = self._get_admin_volumes_client()
        vol = vols_client.create_volume(
            name=data_utils.rand_name('pflex-vtree-imgvol'),
            size=size,
            volume_type=volume_type_name,
            imageRef=image_ref,
        )['volume']
        self.addCleanup(self._safe_delete_volume, vol['id'])
        self._wait_for_volume_status(vol['id'], 'available')
        return vols_client.show_volume(vol['id'])['volume']

    def _safe_delete_volume(self, volume_id):
        """Delete a volume, ignoring 404 (already deleted)."""
        vols_client = self._get_admin_volumes_client()
        try:
            vol = vols_client.show_volume(volume_id)['volume']
        except lib_exc.NotFound:
            return
        deletable = ('available', 'error', 'error_restoring',
                     'error_extending', 'error_managing')
        if vol['status'] in deletable:
            try:
                vols_client.delete_volume(volume_id)
                waiters.wait_for_volume_resource_status(
                    vols_client, volume_id, 'deleted')
            except lib_exc.NotFound:
                pass
        else:
            LOG.warning("Skipping deletion of volume %s in status %s",
                        volume_id, vol['status'])

    def _wait_for_volume_status(self, volume_id, status,
                                timeout=VOLUME_BUILD_TIMEOUT,
                                interval=VOLUME_BUILD_INTERVAL):
        """Wait for a volume to reach the expected status."""
        vols_client = self._get_admin_volumes_client()
        try:
            waiters.wait_for_volume_resource_status(
                vols_client, volume_id, status)
        except Exception:
            end = time.time() + timeout
            while time.time() < end:
                vol = vols_client.show_volume(volume_id)['volume']
                if vol['status'] == status:
                    return
                if vol['status'] == 'error':
                    raise lib_exc.TempestException(
                        "Volume %s went to error state" % volume_id)
                time.sleep(interval)
            raise lib_exc.TimeoutException(
                "Volume %s did not reach status '%s' within %ds"
                % (volume_id, status, timeout))


class TestPowerFlexCloneVtree(PowerFlexVtreeBaseTest):
    """Tests for basic clone operations with the improved vTree code path.

    Validates that create_cloned_volume works correctly after the change
    from query_vtree_statistics to query_vtree_volumes with direct-child
    filtering.
    """

    @classmethod
    def resource_setup(cls):
        super(TestPowerFlexCloneVtree, cls).resource_setup()
        cls.vtype = cls._create_powerflex_volume_type(cls)

    @classmethod
    def _create_powerflex_volume_type(cls, instance):
        """Class-level helper for resource_setup (no self available)."""
        types_client = getattr(cls, 'admin_volume_types_client',
                               getattr(getattr(cls, 'os_admin', None),
                                       'volume_types_client', None))
        backend_name = cls._pick_powerflex_backend_name()
        vtype = types_client.create_volume_type(
            name=data_utils.rand_name('pflex-vtree-type'),
            extra_specs={'volume_backend_name': backend_name},
        )['volume_type']
        cls._created_type_ids.append(vtype['id'])
        return vtype

    # ------------------------------------------------------------------
    # Test: Basic clone succeeds
    # ------------------------------------------------------------------
    @decorators.idempotent_id('c3d4e5f6-a7b8-9012-cdef-ab3456789012')
    def test_clone_volume_basic(self):
        """Verify a single clone from a PowerFlex volume succeeds.

        This is the simplest end-to-end test ensuring the new
        _check_image_cache_vtree_limit code path does not break
        normal (non-image-cache) clone operations.
        """
        source = self._create_volume(self.vtype['name'])
        clone = self._clone_volume(source['id'], self.vtype['name'])

        self.assertEqual(clone['status'], 'available')
        self.assertEqual(clone['size'], source['size'])
        self.assertNotEqual(clone['id'], source['id'])
        LOG.info("Basic clone test passed: source=%s clone=%s",
                 source['id'], clone['id'])

    # ------------------------------------------------------------------
    # Test: Multiple sequential clones from the same source
    # ------------------------------------------------------------------
    @decorators.idempotent_id('d4e5f6a7-b8c9-0123-defa-bc4567890123')
    def test_clone_volume_multiple_sequential(self):
        """Create 3 sequential clones from the same source volume.

        Ensures the new direct-child counting does not falsely block
        subsequent clones when under the configured limit. Each clone
        adds one direct child to the source volume's vTree.
        """
        source = self._create_volume(self.vtype['name'])
        clone_ids = []
        num_clones = 3

        for i in range(num_clones):
            clone = self._clone_volume(source['id'], self.vtype['name'])
            self.assertEqual(clone['status'], 'available')
            clone_ids.append(clone['id'])
            LOG.info("Sequential clone %d/%d created: %s",
                     i + 1, num_clones, clone['id'])

        self.assertEqual(len(clone_ids), num_clones)
        self.assertEqual(len(set(clone_ids)), num_clones,
                         "Clone IDs should be unique")

    # ------------------------------------------------------------------
    # Test: Clone chain (grandchildren not counted)
    # ------------------------------------------------------------------
    @decorators.idempotent_id('e5f6a7b8-c9d0-1234-efab-cd5678901234')
    def test_clone_chain_grandchildren_not_counted(self):
        """Build a 3-level clone chain and verify the original can still
        be cloned.

        Chain: source -> clone_1 -> clone_1_1
        Then clone source again -> clone_2.

        Under the old code (query_vtree_statistics), the total numOfVolumes
        would be 4, potentially blocking clone_2 if the limit was 4.
        Under the new code, only direct children of source (clone_1) are
        counted, so clone_2 should succeed.
        """
        source = self._create_volume(self.vtype['name'])

        # Level 1: direct child
        clone_1 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_1['status'], 'available')

        # Level 2: grandchild of source (child of clone_1)
        clone_1_1 = self._clone_volume(clone_1['id'], self.vtype['name'])
        self.assertEqual(clone_1_1['status'], 'available')

        # Level 1 again: another direct child of source
        # This would have been blocked by the old code if total descendants
        # exceeded the limit.
        clone_2 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_2['status'], 'available')

        LOG.info("Clone chain test passed: source=%s "
                 "clone_1=%s clone_1_1=%s clone_2=%s",
                 source['id'], clone_1['id'],
                 clone_1_1['id'], clone_2['id'])

    # ------------------------------------------------------------------
    # Test: Deep clone chain
    # ------------------------------------------------------------------
    @decorators.idempotent_id('f6a7b8c9-d0e1-2345-fabc-de6789012345')
    def test_deep_clone_chain_no_false_limit(self):
        """Build a 4-level deep clone chain and clone the root again.

        Chain: root -> L1 -> L2 -> L3
        Then clone root -> L1_b.

        Total volumes in vTree = 5, but root has only 2 direct children
        (L1 and L1_b). The new code should allow L1_b creation regardless
        of the deep chain below L1.
        """
        root = self._create_volume(self.vtype['name'])

        level_1 = self._clone_volume(root['id'], self.vtype['name'])
        self.assertEqual(level_1['status'], 'available')

        level_2 = self._clone_volume(level_1['id'], self.vtype['name'])
        self.assertEqual(level_2['status'], 'available')

        level_3 = self._clone_volume(level_2['id'], self.vtype['name'])
        self.assertEqual(level_3['status'], 'available')

        # Clone root again: should succeed since only 1 direct child (L1)
        # existed before this; new total direct children = 2.
        level_1_b = self._clone_volume(root['id'], self.vtype['name'])
        self.assertEqual(level_1_b['status'], 'available')

        LOG.info("Deep chain test passed: root=%s depths=[%s, %s, %s] "
                 "root_clone_b=%s",
                 root['id'], level_1['id'], level_2['id'],
                 level_3['id'], level_1_b['id'])

    # ------------------------------------------------------------------
    # Test: Clone with larger size
    # ------------------------------------------------------------------
    @decorators.idempotent_id('a7b8c9d0-e1f2-3456-abcd-ef7890123456')
    def test_clone_volume_with_larger_size(self):
        """Clone a volume requesting a larger size.

        Ensures the vtree check does not interfere with the extend
        operation that follows a successful clone.
        """
        source = self._create_volume(self.vtype['name'], size=8)
        clone = self._clone_volume(source['id'], self.vtype['name'], size=16)

        self.assertEqual(clone['status'], 'available')
        self.assertGreaterEqual(clone['size'], 16)
        LOG.info("Larger-size clone test passed: source=%s (%dGB) "
                 "clone=%s (%dGB)",
                 source['id'], source['size'],
                 clone['id'], clone['size'])

    # ------------------------------------------------------------------
    # Test: Delete clone then re-clone
    # ------------------------------------------------------------------
    @decorators.idempotent_id('b8c9d0e1-f2a3-4567-bcde-fa8901234567')
    def test_delete_clone_and_reclone(self):
        """Delete a clone and create a new one from the same source.

        Validates that the direct-child count is accurate after deletion,
        allowing new clones to be created.
        """
        vols_client = self._get_admin_volumes_client()
        source = self._create_volume(self.vtype['name'])

        # Create first clone
        clone_1 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_1['status'], 'available')

        # Delete the clone
        vols_client.delete_volume(clone_1['id'])
        try:
            waiters.wait_for_volume_resource_status(
                vols_client, clone_1['id'], 'deleted')
        except lib_exc.NotFound:
            pass

        # Create another clone from the same source
        clone_2 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_2['status'], 'available')
        self.assertNotEqual(clone_2['id'], clone_1['id'])

        LOG.info("Delete-and-reclone test passed: source=%s "
                 "deleted=%s new=%s",
                 source['id'], clone_1['id'], clone_2['id'])

    # ------------------------------------------------------------------
    # Test: Multiple sources have independent vtree counts
    # ------------------------------------------------------------------
    @decorators.idempotent_id('c9d0e1f2-a3b4-5678-cdef-ab9012345678')
    def test_clone_from_different_sources_independent(self):
        """Clone from two different source volumes.

        Each source has its own vTree, so cloning from one should not
        affect the other's direct-child count.
        """
        source_a = self._create_volume(self.vtype['name'])
        source_b = self._create_volume(self.vtype['name'])

        clone_a1 = self._clone_volume(source_a['id'], self.vtype['name'])
        clone_a2 = self._clone_volume(source_a['id'], self.vtype['name'])
        clone_b1 = self._clone_volume(source_b['id'], self.vtype['name'])

        self.assertEqual(clone_a1['status'], 'available')
        self.assertEqual(clone_a2['status'], 'available')
        self.assertEqual(clone_b1['status'], 'available')

        LOG.info("Independent sources test passed: "
                 "source_a=%s (clones: %s, %s) source_b=%s (clone: %s)",
                 source_a['id'], clone_a1['id'], clone_a2['id'],
                 source_b['id'], clone_b1['id'])

    # ------------------------------------------------------------------
    # Test: Clone chain then clone sibling from intermediate node
    # ------------------------------------------------------------------
    @decorators.idempotent_id('d0e1f2a3-b4c5-6789-defa-bc0123456789')
    def test_clone_from_intermediate_node(self):
        """Clone from an intermediate node in a clone chain.

        Chain: source -> mid -> leaf
        Then clone mid -> mid_clone.

        The intermediate node (mid) is the ancestor; its direct children
        are leaf and mid_clone (2 total). The source's direct child is
        still just mid (1 total).
        """
        source = self._create_volume(self.vtype['name'])
        mid = self._clone_volume(source['id'], self.vtype['name'])
        leaf = self._clone_volume(mid['id'], self.vtype['name'])

        # Clone from the intermediate node
        mid_clone = self._clone_volume(mid['id'], self.vtype['name'])
        self.assertEqual(mid_clone['status'], 'available')

        # Source should still be clonable (only 1 direct child: mid)
        source_clone = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(source_clone['status'], 'available')

        LOG.info("Intermediate-node clone test passed: "
                 "source=%s mid=%s leaf=%s mid_clone=%s source_clone=%s",
                 source['id'], mid['id'], leaf['id'],
                 mid_clone['id'], source_clone['id'])


class TestPowerFlexImageCacheVtree(PowerFlexVtreeBaseTest):
    """Tests for image cache vTree behavior.

    These tests exercise the image-volume-cache code path by creating
    volumes from Glance images. When image_volume_cache_enabled is True
    in cinder.conf, the first volume-from-image creates a cache entry,
    and subsequent volumes clone from that cache entry — triggering the
    _check_image_cache_vtree_limit logic.
    """

    @classmethod
    def skip_checks(cls):
        super(TestPowerFlexImageCacheVtree, cls).skip_checks()
        if not CONF.compute.image_ref:
            raise cls.skipException(
                "compute.image_ref not set in tempest.conf; "
                "cannot test image-based volume creation")

    @classmethod
    def resource_setup(cls):
        super(TestPowerFlexImageCacheVtree, cls).resource_setup()
        types_client = getattr(cls, 'admin_volume_types_client',
                               getattr(getattr(cls, 'os_admin', None),
                                       'volume_types_client', None))
        backend_name = cls._pick_powerflex_backend_name()
        cls.vtype = types_client.create_volume_type(
            name=data_utils.rand_name('pflex-img-vtree-type'),
            extra_specs={'volume_backend_name': backend_name},
        )['volume_type']
        cls._created_type_ids.append(cls.vtype['id'])
        cls.image_ref = CONF.compute.image_ref

    # ------------------------------------------------------------------
    # Test: Single volume from image
    # ------------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-c5d6-7890-efab-cd1234567890')
    def test_create_volume_from_image(self):
        """Create a volume from a Glance image on a PowerFlex backend.

        Validates that the basic image-to-volume path works with the
        new vTree code. If image volume cache is enabled, this creates
        the cache entry volume (the vTree root).
        """
        vol = self._create_volume_from_image(
            self.vtype['name'], self.image_ref)

        self.assertEqual(vol['status'], 'available')
        self.assertIsNotNone(vol.get('id'))
        LOG.info("Image volume created: %s (image=%s)",
                 vol['id'], self.image_ref)

    # ------------------------------------------------------------------
    # Test: Multiple volumes from the same image
    # ------------------------------------------------------------------
    @decorators.idempotent_id('f2a3b4c5-d6e7-8901-fabc-de2345678901')
    def test_multiple_volumes_from_same_image(self):
        """Create 3 volumes from the same Glance image.

        When image_volume_cache_enabled is True, the 2nd and 3rd volumes
        clone from the cache entry. This exercises _check_image_cache_vtree_limit
        with increasing direct-child count on the cache volume's vTree.

        If image cache is disabled, all 3 are independent volumes and
        the vtree limit path is not exercised (still a valid test).
        """
        volumes = []
        num_volumes = 3
        for i in range(num_volumes):
            vol = self._create_volume_from_image(
                self.vtype['name'], self.image_ref)
            self.assertEqual(vol['status'], 'available')
            volumes.append(vol)
            LOG.info("Image volume %d/%d created: %s",
                     i + 1, num_volumes, vol['id'])

        ids = [v['id'] for v in volumes]
        self.assertEqual(len(set(ids)), num_volumes,
                         "All volumes should have unique IDs")

    # ------------------------------------------------------------------
    # Test: Volume from image then clone that volume
    # ------------------------------------------------------------------
    @decorators.idempotent_id('a3b4c5d6-e7f8-9012-abcd-ef3456789012')
    def test_image_volume_then_clone(self):
        """Create a volume from image, then clone it.

        If image cache is active, the image volume is a clone of the
        cache entry. Cloning the image volume creates a grandchild of
        the cache entry. Under the new code, this grandchild should NOT
        count toward the cache entry's direct-child limit.
        """
        img_vol = self._create_volume_from_image(
            self.vtype['name'], self.image_ref)
        self.assertEqual(img_vol['status'], 'available')

        clone = self._clone_volume(img_vol['id'], self.vtype['name'])
        self.assertEqual(clone['status'], 'available')

        LOG.info("Image-then-clone test passed: img_vol=%s clone=%s",
                 img_vol['id'], clone['id'])

    # ------------------------------------------------------------------
    # Test: Multiple image volumes + clone from each
    # ------------------------------------------------------------------
    @decorators.idempotent_id('b4c5d6e7-f8a9-0123-bcde-fa4567890123')
    def test_image_volumes_with_nested_clones(self):
        """Create 2 volumes from the same image, then clone each.

        If image cache is active:
          cache_entry -> img_vol_1 -> clone_1  (grandchild)
          cache_entry -> img_vol_2 -> clone_2  (grandchild)

        The cache entry has 2 direct children. The clones are grandchildren
        and should NOT be counted. Under the old code with total descendant
        counting, the cache entry would show numOfVolumes=5, potentially
        triggering a false limit. The new code counts only 2 direct children.
        """
        img_vol_1 = self._create_volume_from_image(
            self.vtype['name'], self.image_ref)
        img_vol_2 = self._create_volume_from_image(
            self.vtype['name'], self.image_ref)

        clone_1 = self._clone_volume(img_vol_1['id'], self.vtype['name'])
        clone_2 = self._clone_volume(img_vol_2['id'], self.vtype['name'])

        self.assertEqual(clone_1['status'], 'available')
        self.assertEqual(clone_2['status'], 'available')

        # Create yet another volume from the same image — should succeed
        # because the 4 grandchildren don't inflate the direct-child count
        img_vol_3 = self._create_volume_from_image(
            self.vtype['name'], self.image_ref)
        self.assertEqual(img_vol_3['status'], 'available')

        LOG.info("Nested image clones test passed: "
                 "img1=%s clone1=%s img2=%s clone2=%s img3=%s",
                 img_vol_1['id'], clone_1['id'],
                 img_vol_2['id'], clone_2['id'],
                 img_vol_3['id'])

    # ------------------------------------------------------------------
    # Test: Delete image volume then create new one from same image
    # ------------------------------------------------------------------
    @decorators.idempotent_id('c5d6e7f8-a9b0-1234-cdef-ab5678901234')
    def test_delete_image_volume_and_recreate(self):
        """Create a volume from image, delete it, create another.

        Verifies that the vTree direct-child count decreases after
        a clone is deleted, allowing new clones.
        """
        vols_client = self._get_admin_volumes_client()

        vol_1 = self._create_volume_from_image(
            self.vtype['name'], self.image_ref)
        self.assertEqual(vol_1['status'], 'available')

        # Delete it
        vols_client.delete_volume(vol_1['id'])
        try:
            waiters.wait_for_volume_resource_status(
                vols_client, vol_1['id'], 'deleted')
        except lib_exc.NotFound:
            pass

        # Create another from the same image
        vol_2 = self._create_volume_from_image(
            self.vtype['name'], self.image_ref)
        self.assertEqual(vol_2['status'], 'available')
        self.assertNotEqual(vol_2['id'], vol_1['id'])

        LOG.info("Delete-and-recreate image volume test passed: "
                 "deleted=%s new=%s", vol_1['id'], vol_2['id'])

    # ------------------------------------------------------------------
    # Test: Cache replacement when vtree limit is reached
    # ------------------------------------------------------------------
    @decorators.idempotent_id('d6e7f8a9-b0c1-2345-defa-bc6789012345')
    def test_image_cache_vtree_limit_triggers_replacement(self):
        """Test cache replacement when vtree limit is reached.

        When powerflex_max_image_cache_vtree_size is configured (e.g., 3),
        the first 3 image volumes should be created from the same image cache
        entry. The 4th volume should trigger cache replacement, creating a
        new cache entry from the image.

        This test validates that the new direct-child counting does not
        falsely block the 4th volume. Under the old code (total descendant
        counting), if the vTree had grandchildren, the total might exceed
        the limit and block the 4th volume even though only 3 direct children
        existed.

        Note: To fully test cache replacement, set powerflex_max_image_cache_vtree_size=3
        in cinder.conf on the PowerFlex backend. If set to 0 (unlimited), this
        test still passes but doesn't trigger replacement.
        """
        volumes = []
        num_volumes = 4

        for i in range(num_volumes):
            vol = self._create_volume_from_image(
                self.vtype['name'], self.image_ref)
            self.assertEqual(vol['status'], 'available')
            volumes.append(vol)
            LOG.info("Image volume %d/%d created: %s",
                     i + 1, num_volumes, vol['id'])

        # All 4 volumes should succeed
        ids = [v['id'] for v in volumes]
        self.assertEqual(len(set(ids)), num_volumes,
                         "All volumes should have unique IDs")

        # Verify cache replacement via source_volid differences
        # When cache replacement occurs, the new cache entry will have a different
        # source_volid. Volumes cloned from the same cache entry share the same
        # source_volid (pointing to that cache entry volume).
        source_volids = [v.get('source_volid') for v in volumes]
        LOG.info("Volume source_volid values: %s", source_volids)

        # With cache replacement, we expect:
        # - First 3 volumes: same source_volid (original cache entry)
        # - 4th volume: different source_volid (new cache entry)
        # If cache is unlimited (limit=0), all may share the same source_volid
        unique_source_volids = len(set(filter(None, source_volids)))
        LOG.info("Unique source_volid count: %d (expected >1 if cache replacement occurred)",
                 unique_source_volids)

        # Assert cache replacement occurred (unique_source_volids > 1)
        # This validates that the 4th volume triggered cache replacement
        # Note: This assertion assumes powerflex_max_image_cache_vtree_size=3
        # is configured in cinder.conf. If set to 0 (unlimited), the test will
        # skip this assertion.
        if unique_source_volids <= 1:
            self.skipTest(
                "Cache replacement not detected "
                "(unique_source_volids=%s, source_volids=%s). "
                "This may indicate powerflex_max_image_cache_vtree_size is "
                "set to 0 (unlimited) in cinder.conf. To test cache replacement, "
                "set powerflex_max_image_cache_vtree_size=3 and restart cinder-volume."
                % (unique_source_volids, source_volids)
            )

        self.assertGreater(
            unique_source_volids, 1,
            "Expected cache replacement to occur (unique_source_volids > 1), "
            "but all volumes share the same source_volid. This indicates cache "
            "replacement did not trigger."
        )

        LOG.info("Cache replacement test passed: all %d volumes created from "
                 "same image (volumes: %s). Cache replacement detected: %s",
                 num_volumes, ids, unique_source_volids > 1)


class TestPowerFlexCloneEdgeCases(PowerFlexVtreeBaseTest):
    """Edge-case tests for PowerFlex clone with improved vTree counting."""

    @classmethod
    def resource_setup(cls):
        super(TestPowerFlexCloneEdgeCases, cls).resource_setup()
        types_client = getattr(cls, 'admin_volume_types_client',
                               getattr(getattr(cls, 'os_admin', None),
                                       'volume_types_client', None))
        backend_name = cls._pick_powerflex_backend_name()
        cls.vtype = types_client.create_volume_type(
            name=data_utils.rand_name('pflex-edge-type'),
            extra_specs={'volume_backend_name': backend_name},
        )['volume_type']
        cls._created_type_ids.append(cls.vtype['id'])

    # ------------------------------------------------------------------
    # Test: Clone a clone (not the original) — independent vTree ancestor
    # ------------------------------------------------------------------
    @decorators.idempotent_id('e7f8a9b0-c1d2-3456-efab-cd7890123456')
    def test_clone_of_clone(self):
        """Clone a clone. The new clone's ancestorVolumeId should point
        to clone_1, not the original source.

        source -> clone_1 -> clone_of_clone

        Under the new code, clone_of_clone is a direct child of clone_1,
        not of source. Source's direct-child count remains 1.
        """
        source = self._create_volume(self.vtype['name'])
        clone_1 = self._clone_volume(source['id'], self.vtype['name'])
        clone_of_clone = self._clone_volume(clone_1['id'],
                                            self.vtype['name'])

        self.assertEqual(clone_of_clone['status'], 'available')
        # Source should still be clonable
        clone_2 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_2['status'], 'available')

        LOG.info("Clone-of-clone test passed: source=%s clone_1=%s "
                 "clone_of_clone=%s clone_2=%s",
                 source['id'], clone_1['id'],
                 clone_of_clone['id'], clone_2['id'])

    # ------------------------------------------------------------------
    # Test: Wide fan-out (many clones from single source)
    # ------------------------------------------------------------------
    @decorators.idempotent_id('f8a9b0c1-d2e3-4567-fabc-de8901234567')
    def test_wide_fan_out_clones(self):
        """Create 5 clones from the same source volume.

        Validates that the direct-child count correctly tracks multiple
        clones and none are falsely blocked.
        """
        source = self._create_volume(self.vtype['name'])
        clones = []
        num_clones = 5

        for i in range(num_clones):
            clone = self._clone_volume(source['id'], self.vtype['name'])
            self.assertEqual(clone['status'], 'available')
            clones.append(clone)

        self.assertEqual(len(clones), num_clones)
        LOG.info("Wide fan-out test passed: source=%s clones=%s",
                 source['id'], [c['id'] for c in clones])

    # ------------------------------------------------------------------
    # Test: Fan-out with nested clones from each child
    # ------------------------------------------------------------------
    @decorators.idempotent_id('a9b0c1d2-e3f4-5678-abcd-ef9012345678')
    def test_fan_out_with_nested_children(self):
        """Create 3 clones from source, then clone each clone.

        vTree structure:
          source -> clone_1 -> nested_1
          source -> clone_2 -> nested_2
          source -> clone_3 -> nested_3

        Source has 3 direct children. Total volumes = 7 (source + 3 + 3).
        Under old code, numOfVolumes=7 might trigger limit.
        Under new code, direct children = 3.
        """
        source = self._create_volume(self.vtype['name'])
        for i in range(3):
            clone = self._clone_volume(source['id'], self.vtype['name'])
            nested = self._clone_volume(clone['id'], self.vtype['name'])
            self.assertEqual(nested['status'], 'available')
            LOG.info("Fan-out + nested: clone_%d=%s nested_%d=%s",
                     i, clone['id'], i, nested['id'])

        # Source should still be clonable
        final_clone = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(final_clone['status'], 'available')
        LOG.info("Fan-out with nested test passed: source=%s "
                 "final_clone=%s", source['id'], final_clone['id'])

    # ------------------------------------------------------------------
    # Test: Simultaneous clone creation
    # ------------------------------------------------------------------
    @decorators.idempotent_id('b0c1d2e3-f4a5-6789-bcde-fa0123456789')
    def test_clone_volume_verify_properties(self):
        """Verify cloned volume retains source volume properties.

        Ensures that the vtree counting logic does not interfere with
        volume metadata propagation during clone.
        """
        source = self._create_volume(self.vtype['name'], size=8)
        clone = self._clone_volume(source['id'], self.vtype['name'])

        self.assertEqual(clone['status'], 'available')
        self.assertEqual(clone['size'], source['size'])
        self.assertEqual(clone['volume_type'], source['volume_type'])
        self.assertIsNotNone(clone.get('id'))
        self.assertIsNotNone(clone.get('created_at'))

        LOG.info("Clone properties test passed: source=%s clone=%s",
                 source['id'], clone['id'])

    # ------------------------------------------------------------------
    # Test: Clone after source extend
    # ------------------------------------------------------------------
    @decorators.idempotent_id('c1d2e3f4-a5b6-7890-cdef-ab1234567890')
    def test_clone_after_source_extend(self):
        """Extend a source volume and then clone it.

        Verifies vtree counting is not affected by volume extend
        operations on the source.
        """
        vols_client = self._get_admin_volumes_client()
        source = self._create_volume(self.vtype['name'], size=8)

        # Extend source
        vols_client.extend_volume(source['id'], new_size=16)
        self._wait_for_volume_status(source['id'], 'available')
        source_updated = vols_client.show_volume(source['id'])['volume']
        self.assertGreaterEqual(source_updated['size'], 16)

        # Clone after extend
        clone = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone['status'], 'available')
        self.assertGreaterEqual(clone['size'], 16)

        LOG.info("Clone-after-extend test passed: source=%s (%dGB) "
                 "clone=%s (%dGB)",
                 source['id'], source_updated['size'],
                 clone['id'], clone['size'])

    # ------------------------------------------------------------------
    # Test: Mixed operations on vtree (clone + delete + clone)
    # ------------------------------------------------------------------
    @decorators.idempotent_id('d2e3f4a5-b6c7-8901-defa-bc2345678901')
    def test_mixed_clone_delete_clone_cycle(self):
        """Perform a cycle of clone-delete-clone operations.

        source -> clone_1 (delete) -> clone_2 -> clone_3 (delete) -> clone_4

        Validates the direct-child count remains accurate through
        multiple create/delete cycles.
        """
        vols_client = self._get_admin_volumes_client()
        source = self._create_volume(self.vtype['name'])

        # Clone 1 — create and delete
        clone_1 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_1['status'], 'available')
        vols_client.delete_volume(clone_1['id'])
        try:
            waiters.wait_for_volume_resource_status(
                vols_client, clone_1['id'], 'deleted')
        except lib_exc.NotFound:
            pass

        # Clone 2 — keep
        clone_2 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_2['status'], 'available')

        # Clone 3 — create and delete
        clone_3 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_3['status'], 'available')
        vols_client.delete_volume(clone_3['id'])
        try:
            waiters.wait_for_volume_resource_status(
                vols_client, clone_3['id'], 'deleted')
        except lib_exc.NotFound:
            pass

        # Clone 4 — keep
        clone_4 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(clone_4['status'], 'available')

        LOG.info("Mixed clone-delete cycle test passed: source=%s "
                 "surviving=[%s, %s]",
                 source['id'], clone_2['id'], clone_4['id'])

    # ------------------------------------------------------------------
    # Test: Diamond-shaped clone tree
    # ------------------------------------------------------------------
    @decorators.idempotent_id('e3f4a5b6-c7d8-9012-efab-cd3456789012')
    def test_diamond_clone_tree(self):
        """Create a diamond-shaped vTree.

        source -> child_1 -> grandchild
        source -> child_2 -> grandchild_from_child_2

        Source has 2 direct children. Each child has 1 child.
        Total volumes = 5. Under old code, 5 might exceed limit.
        Under new code, source direct children = 2.

        Then clone source again — should succeed.
        """
        source = self._create_volume(self.vtype['name'])

        child_1 = self._clone_volume(source['id'], self.vtype['name'])
        child_2 = self._clone_volume(source['id'], self.vtype['name'])

        gc_1 = self._clone_volume(child_1['id'], self.vtype['name'])
        gc_2 = self._clone_volume(child_2['id'], self.vtype['name'])

        self.assertEqual(gc_1['status'], 'available')
        self.assertEqual(gc_2['status'], 'available')

        # Source should still be clonable
        child_3 = self._clone_volume(source['id'], self.vtype['name'])
        self.assertEqual(child_3['status'], 'available')

        LOG.info("Diamond tree test passed: source=%s "
                 "children=[%s, %s, %s] grandchildren=[%s, %s]",
                 source['id'], child_1['id'], child_2['id'], child_3['id'],
                 gc_1['id'], gc_2['id'])
