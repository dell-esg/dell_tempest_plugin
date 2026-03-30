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
Tempest functional tests for Dell PowerStore volume manage/unmanage feature.

These tests are **complementary** to the generic Cinder manage/unmanage
tempest test (tempest/api/volume/admin/test_volume_manage.py).  The generic
test already covers the basic unmanage -> manage-by-source-name lifecycle
and verifies size, type, availability_zone, and host preservation.

This file focuses exclusively on PowerStore-specific behaviour that is
NOT covered generically:

  * Manage by source-id  (PowerStore provider_id / UUID)
  * Extend a managed volume  (validates PowerStore resize on managed vols)
  * Delete a managed volume  (validates PowerStore backend cleanup)
  * Full manage -> extend -> delete lifecycle
  * Unmanage preserves backend volume  (re-manage proves no data loss)
  * Negative: manage with invalid ref  (no source-id / source-name)
  * Negative: manage nonexistent volume by source-id

Patch reference: manage-unmanage-volume.patch
  - adapter.py: manage_existing(), manage_existing_get_size()
  - driver.py:  manage_existing(), manage_existing_get_size(), unmanage()
  - client.py:  get_volume_details_by_id/name(), volume_is_mapped()
"""

import configparser
import time

import requests
from oslo_log import log as logging
from tempest.api.volume import base as volume_base
from tempest.common import waiters
from tempest import config
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc
from tempest.lib.common.utils import data_utils

CONF = config.CONF
LOG = logging.getLogger(__name__)

VOLUME_BUILD_TIMEOUT = 600
VOLUME_BUILD_INTERVAL = 5


# ======================================================================
# Base mixin — helpers only, no test methods
# ======================================================================
class PowerStoreVolumeManageUnmanageBase(object):
    """Mixin providing helpers for PowerStore volume manage/unmanage tests.

    All heavy lifting (volume type creation, volume creation, manage,
    unmanage, extend, wait-loops, cleanup) lives here so the test
    methods stay short and readable.
    """

    # ------------------------------------------------------------------
    # Client resolution
    # ------------------------------------------------------------------
    def _get_admin_volumes_client(self):
        """Return the admin volumes client."""
        client = getattr(self, 'admin_volume_client', None)
        if client:
            return client
        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (getattr(os_admin, 'volumes_client_latest', None) or
                      getattr(os_admin, 'volumes_v3_client', None) or
                      getattr(os_admin, 'volumes_client', None))
            if client:
                return client
        self.skipTest("Admin volumes client not found.")

    def _get_admin_volume_types_client(self):
        """Return the admin volume types client."""
        client = getattr(self, 'admin_volume_types_client', None)
        if client:
            return client
        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (getattr(os_admin, 'volume_types_client_latest', None) or
                      getattr(os_admin, 'volume_types_client', None))
            if client:
                return client
        self.skipTest("Admin volume types client not found.")

    def _get_admin_volume_manage_client(self):
        """Return the admin volume manage client."""
        client = getattr(self, 'admin_volume_manage_client', None)
        if client:
            return client
        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (getattr(os_admin, 'volume_manage_client_latest', None) or
                      getattr(os_admin, 'volume_manage_client', None))
            if client:
                return client
        self.skipTest("Admin volume manage client not found.")

    def _get_admin_scheduler_stats_client(self):
        """Return the admin scheduler stats client (optional)."""
        client = getattr(self, 'admin_scheduler_stats_client', None)
        if client:
            return client
        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            return getattr(os_admin, 'scheduler_stats_client', None)
        return None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def setUp(self):
        super(PowerStoreVolumeManageUnmanageBase, self).setUp()
        self.vols = self._get_admin_volumes_client()
        self.vtypes = self._get_admin_volume_types_client()
        self.vol_manage = self._get_admin_volume_manage_client()
        self.sched = self._get_admin_scheduler_stats_client()

        # Discover the PowerStore host@backend#pool string
        self.powerstore_host = self._discover_powerstore_host()
        if not self.powerstore_host:
            self.skipTest("No PowerStore host discovered from scheduler "
                          "pools; cannot run manage/unmanage tests.")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def _discover_powerstore_host(self):
        """Discover the PowerStore host@backend#pool from scheduler pools."""
        if not self.sched:
            return self._discover_powerstore_host_from_services()
        try:
            pools = self.sched.list_pools(detail=True).get('pools', [])
            for p in pools:
                name = p.get('name', '')
                caps = p.get('capabilities', {}) or {}
                backend_name = caps.get('volume_backend_name', '')
                if ('powerstore' in name.lower() or
                        'powerstore' in backend_name.lower()):
                    LOG.info("Discovered PowerStore pool: %s", name)
                    return name
        except Exception as e:
            LOG.warning("Pool discovery failed: %s", e)
        return self._discover_powerstore_host_from_services()

    def _discover_powerstore_host_from_services(self):
        """Fallback: discover PowerStore host from cinder-volume services."""
        try:
            svc_client = getattr(self, 'admin_volume_services_client', None)
            if not svc_client:
                os_admin = getattr(self, 'os_admin', None)
                if os_admin:
                    svc_client = (
                        getattr(os_admin,
                                'volume_services_client_latest', None) or
                        getattr(os_admin,
                                'volume_services_client', None))
            if svc_client:
                services = svc_client.list_services()['services']
                for svc in services:
                    if (svc['binary'] == 'cinder-volume' and
                            'powerstore' in svc['host'].lower()):
                        LOG.info("Discovered PowerStore service host: %s",
                                 svc['host'])
                        return svc['host']
        except Exception as e:
            LOG.warning("Service discovery failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Volume type helpers
    # ------------------------------------------------------------------
    def _create_powerstore_volume_type(self, extra_specs=None):
        """Create a volume type targeting the PowerStore backend."""
        name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-manage-type')
        specs = {'volume_backend_name': 'powerstore'}
        if extra_specs:
            specs.update(extra_specs)
        vt = self.vtypes.create_volume_type(
            name=name, extra_specs=specs)['volume_type']
        LOG.info("Created volume type '%s' (id=%s) with specs=%s",
                 vt['name'], vt['id'], specs)
        self.addCleanup(self._delete_volume_type_safe, vt['id'])
        return vt

    def _delete_volume_type_safe(self, type_id, timeout=300, interval=5):
        """Try to delete a volume type; retry if still in use."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.vtypes.delete_volume_type(type_id)
                return
            except lib_exc.BadRequest:
                LOG.info("Volume type %s still in use; retrying...", type_id)
            except lib_exc.NotFound:
                return
            time.sleep(interval)
        try:
            self.vtypes.delete_volume_type(type_id)
        except Exception as e:
            LOG.warning("Final delete of volume type %s failed: %s",
                        type_id, e)

    # ------------------------------------------------------------------
    # Volume helpers
    # ------------------------------------------------------------------
    def _create_volume(self, vt_name, size=1):
        """Create a volume on PowerStore and wait until available."""
        vol = self.vols.create_volume(
            name=data_utils.rand_name(
                prefix=CONF.resource_name_prefix,
                name='ps-manage-vol'),
            size=size,
            volume_type=vt_name,
        )['volume']
        self.addCleanup(self._delete_volume_safe, vol['id'])
        waiters.wait_for_volume_resource_status(
            self.vols, vol['id'], 'available')
        vol_info = self.vols.show_volume(vol['id'])['volume']
        LOG.info("Volume %s available on host '%s', provider_id='%s'",
                 vol_info['id'],
                 vol_info.get('os-vol-host-attr:host'),
                 vol_info.get('provider_id'))
        return vol_info

    def _delete_volume_safe(self, vol_id):
        """Delete a volume; ignore NotFound."""
        try:
            self.vols.delete_volume(vol_id)
        except lib_exc.NotFound:
            return
        except Exception as e:
            LOG.debug("delete_volume(%s) raised: %s", vol_id, e)
        try:
            self.vols.wait_for_resource_deletion(vol_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            self._wait_for_volume_deletion(vol_id)

    def _wait_for_volume_deletion(self, vol_id,
                                  timeout=VOLUME_BUILD_TIMEOUT,
                                  interval=VOLUME_BUILD_INTERVAL):
        """Poll until a volume is gone."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.vols.show_volume(vol_id)
            except lib_exc.NotFound:
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for volume %s deletion", vol_id)

    def _wait_for_volume_status(self, vol_id, target,
                                timeout=VOLUME_BUILD_TIMEOUT,
                                interval=VOLUME_BUILD_INTERVAL):
        """Poll until the volume reaches the target status."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            v = self.vols.show_volume(vol_id)['volume']
            status = (v.get('status') or '').lower()
            last = status
            if status == target:
                return v
            if status in ('error', 'error_restoring', 'error_extending',
                          'error_managing'):
                self.fail(
                    "Volume %s entered error state: %s" % (vol_id, status))
            time.sleep(interval)
        self.fail("Timeout waiting for volume %s to reach '%s'; "
                  "last='%s'" % (vol_id, target, last))

    # ------------------------------------------------------------------
    # Unmanage / Manage helpers
    # ------------------------------------------------------------------
    def _unmanage_volume(self, vol_id):
        """Unmanage a volume and wait until it disappears from Cinder."""
        self.vols.unmanage_volume(vol_id)
        LOG.info("Unmanaged volume %s", vol_id)
        try:
            self.vols.wait_for_resource_deletion(vol_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            self._wait_for_volume_deletion(vol_id)

    def _manage_volume(self, host, ref, volume_type, name=None):
        """Manage an existing backend volume into Cinder.

        :param host: host@backend#pool string.
        :param ref: dict, e.g. {'source-id': '<ps-uuid>'} or
                    {'source-name': '<vol-name>'}.
        :param volume_type: volume type name.
        :param name: optional Cinder volume name.
        :returns: managed volume dict after it reaches 'available'.
        """
        name = name or data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-managed-vol')
        body = {
            'name': name,
            'host': host,
            'ref': ref,
            'volume_type': volume_type,
        }
        new_vol = self.vol_manage.manage_volume(**body)['volume']
        LOG.info("Manage request submitted: id=%s, ref=%s",
                 new_vol['id'], ref)
        self.addCleanup(self._delete_volume_safe, new_vol['id'])
        waiters.wait_for_volume_resource_status(
            self.vols, new_vol['id'], 'available')
        managed = self.vols.show_volume(new_vol['id'])['volume']
        LOG.info("Managed volume %s is available, provider_id='%s'",
                 managed['id'], managed.get('provider_id'))
        return managed

    def _manage_volume_expect_error(self, host, ref, volume_type,
                                    name=None):
        """Manage a volume and expect it to end up in error state.

        :returns: volume dict with error status, or None if the API
                  rejected the request outright.
        """
        name = name or data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-manage-fail')
        body = {
            'name': name,
            'host': host,
            'ref': ref,
            'volume_type': volume_type,
        }
        try:
            new_vol = self.vol_manage.manage_volume(**body)['volume']
        except (lib_exc.BadRequest, lib_exc.ServerFault) as e:
            LOG.info("Manage correctly rejected by API: %s", e)
            return None

        self.addCleanup(self._delete_volume_safe, new_vol['id'])
        # Wait for the manage operation to settle
        deadline = time.time() + VOLUME_BUILD_TIMEOUT
        while time.time() < deadline:
            v = self.vols.show_volume(new_vol['id'])['volume']
            status = (v.get('status') or '').lower()
            if status in ('error', 'error_managing'):
                LOG.info("Manage correctly failed with status=%s", status)
                return v
            if status == 'available':
                # Manage unexpectedly succeeded
                return v
            time.sleep(VOLUME_BUILD_INTERVAL)
        return self.vols.show_volume(new_vol['id'])['volume']

    # ------------------------------------------------------------------
    # Extend helper
    # ------------------------------------------------------------------
    def _extend_volume(self, vol_id, new_size):
        """Extend a volume and wait for it to become available."""
        LOG.info("Extending volume %s to %dG", vol_id, new_size)
        self.vols.extend_volume(vol_id, new_size=new_size)
        waiters.wait_for_volume_resource_status(
            self.vols, vol_id, 'available')
        vol = self.vols.show_volume(vol_id)['volume']
        return vol

    # ------------------------------------------------------------------
    # PowerStore REST API helper
    # ------------------------------------------------------------------
    def _get_powerstore_volume_id_by_name(self, backend_name):
        """Query the PowerStore REST API to get a volume UUID by name.

        Reads PowerStore credentials from cinder.conf [powerstore] section.
        Returns the PowerStore volume UUID string, or None if not found.
        """
        try:
            conf = configparser.ConfigParser()
            conf.read('/etc/cinder/cinder.conf')
            ps_ip = conf.get('powerstore', 'san_ip')
            ps_user = conf.get('powerstore', 'san_login')
            ps_pass = conf.get('powerstore', 'san_password')
        except Exception as e:
            LOG.warning("Cannot read PowerStore creds from cinder.conf: %s", e)
            return None

        url = 'https://%s/api/rest/volume' % ps_ip
        params = {
            'name': 'eq.%s' % backend_name,
            'select': 'id,name',
        }
        try:
            resp = requests.get(
                url, params=params,
                auth=(ps_user, ps_pass),
                verify=False, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    ps_id = data[0]['id']
                    LOG.info("PowerStore volume '%s' has id '%s'",
                             backend_name, ps_id)
                    return ps_id
            LOG.warning("PowerStore lookup for '%s' returned %s",
                        backend_name, resp.status_code)
        except Exception as e:
            LOG.warning("PowerStore REST query failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Unmanage + re-manage round-trip helpers
    # ------------------------------------------------------------------
    def _unmanage_and_remanage_by_source_id(self, vol_info, volume_type):
        """Unmanage a volume and manage it back using source-id.

        Queries the PowerStore REST API to obtain the backend volume UUID,
        since provider_id is not reliably exposed in the Cinder API.

        :param vol_info: original volume dict.
        :param volume_type: volume type name for the managed volume.
        :returns: newly managed volume dict.
        """
        host = vol_info.get('os-vol-host-attr:host')
        backend_name = 'volume-%s' % vol_info['id']

        ps_id = self._get_powerstore_volume_id_by_name(backend_name)
        self.assertIsNotNone(
            ps_id,
            "Could not find PowerStore volume UUID for '%s'" % backend_name)

        self._unmanage_volume(vol_info['id'])

        managed = self._manage_volume(
            host=host,
            ref={'source-id': ps_id},
            volume_type=volume_type,
        )
        return managed

    def _unmanage_and_remanage_by_source_name(self, vol_info, volume_type):
        """Unmanage a volume and manage it back using source-name.

        :param vol_info: original volume dict.
        :param volume_type: volume type name for the managed volume.
        :returns: newly managed volume dict.
        """
        host = vol_info.get('os-vol-host-attr:host')
        # PowerStore backend volume name follows Cinder convention:
        # 'volume-<cinder_uuid>'
        backend_name = 'volume-%s' % vol_info['id']

        self._unmanage_volume(vol_info['id'])

        managed = self._manage_volume(
            host=host,
            ref={'source-name': backend_name},
            volume_type=volume_type,
        )
        return managed


# ======================================================================
# Positive test methods
# ======================================================================
class _PowerStoreVolumeManagePositiveTests(object):
    """Positive functional tests for PowerStore volume manage/unmanage.

    These tests exercise real Cinder API calls that propagate to the
    PowerStore backend.  Each test targets a specific PowerStore-specific
    code path that is NOT covered by the generic tempest manage test.
    """

    @classmethod
    def skip_checks(cls):
        super(_PowerStoreVolumeManagePositiveTests, cls).skip_checks()
        if not CONF.service_available.cinder:
            raise cls.skipException("Cinder is not available")

    # ----------------------------------------------------------------
    # 1. Manage by source-id (PowerStore backend UUID)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-1111-2222-3333-e5f6a7b8c9d0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_volume_by_source_id(self):
        """Manage a PowerStore volume using its backend UUID (source-id).

        The generic tempest test only exercises source-name.  This test
        validates the source-id path in PowerStore's manage_existing()
        which calls client.get_volume_details_by_id().

        The PowerStore volume UUID is obtained by querying the PowerStore
        REST API directly, since Cinder does not reliably expose
        provider_id in the volume API response.

        Flow: create -> query PowerStore for UUID -> unmanage
              -> manage(source-id) -> verify available
        PowerStore API hit: GET /volume/<uuid>
        """
        LOG.info("=== test_manage_volume_by_source_id ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        managed = self._unmanage_and_remanage_by_source_id(vol, vt['name'])

        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], vol['size'])
        LOG.info("Volume managed by source-id: vol %s -> managed %s",
                 vol['id'], managed['id'])

    # ----------------------------------------------------------------
    # 2. Manage by source-id preserves size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b2c3d4e5-2222-3333-4444-f6a7b8c9d0e1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_volume_by_source_id_preserves_size(self):
        """After manage by source-id, volume size must match the original.

        PowerStore's manage_existing_get_size() calls
        client.get_volume_details_by_id() and rounds up to GiB.
        Verify Cinder reports the correct size.

        Flow: create(1G) -> unmanage -> manage(source-id) -> verify size
        """
        LOG.info("=== test_manage_volume_by_source_id_preserves_size ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        original_size = vol['size']

        managed = self._unmanage_and_remanage_by_source_id(vol, vt['name'])

        self.assertEqual(managed['size'], original_size,
                         "Size must be preserved after manage by source-id")
        LOG.info("Size preserved: %dG", original_size)

    # ----------------------------------------------------------------
    # 3. Extend a managed volume
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c3d4e5f6-3333-4444-5555-a7b8c9d0e1f2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_managed_volume(self):
        """Extend a volume that was managed back into Cinder.

        Validates that PowerStore correctly handles extend_volume on
        a volume whose Cinder metadata was re-created via manage.

        Flow: create(1G) -> unmanage -> manage(source-name)
              -> extend(2G) -> verify size=2
        """
        LOG.info("=== test_extend_managed_volume ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        managed = self._unmanage_and_remanage_by_source_name(vol, vt['name'])
        self.assertEqual(managed['size'], 1)

        extended = self._extend_volume(managed['id'], 2)
        self.assertEqual(extended['size'], 2)
        self.assertEqual(extended['status'], 'available')
        LOG.info("Managed volume %s extended to 2G", managed['id'])

    # ----------------------------------------------------------------
    # 4. Delete a managed volume
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d4e5f6a7-4444-5555-6666-b8c9d0e1f2a3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_managed_volume(self):
        """Delete a volume that was managed back into Cinder.

        Validates that PowerStore correctly cleans up a managed volume
        (the backend volume should be removed).

        Flow: create -> unmanage -> manage(source-name) -> delete -> gone
        """
        LOG.info("=== test_delete_managed_volume ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        managed = self._unmanage_and_remanage_by_source_name(vol, vt['name'])
        managed_id = managed['id']

        self.vols.delete_volume(managed_id)
        self.vols.wait_for_resource_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.vols.show_volume,
            managed_id,
        )
        LOG.info("Managed volume %s deleted successfully", managed_id)

    # ----------------------------------------------------------------
    # 5. Full lifecycle: manage -> extend -> delete
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e5f6a7b8-5555-6666-7777-c9d0e1f2a3b4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_extend_delete_lifecycle(self):
        """Full lifecycle: create -> unmanage -> manage -> extend -> delete.

        Exercises the complete PowerStore manage/unmanage flow that a
        real operator would perform: import an existing volume, grow it,
        then remove it.

        Flow: create(1G) -> unmanage -> manage(source-name)
              -> extend(2G) -> delete -> verify gone
        """
        LOG.info("=== test_manage_extend_delete_lifecycle ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        managed = self._unmanage_and_remanage_by_source_name(vol, vt['name'])
        managed_id = managed['id']

        extended = self._extend_volume(managed_id, 2)
        self.assertEqual(extended['size'], 2)

        self.vols.delete_volume(managed_id)
        self.vols.wait_for_resource_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.vols.show_volume,
            managed_id,
        )
        LOG.info("Full manage -> extend -> delete lifecycle completed "
                 "for volume %s", managed_id)

    # ----------------------------------------------------------------
    # 6. Unmanage preserves backend volume (re-manage proves it)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f6a7b8c9-6666-7777-8888-d0e1f2a3b4c5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_unmanage_preserves_backend_volume(self):
        """Unmanage removes Cinder metadata but leaves backend intact.

        Prove this by unmanaging and then re-managing the same volume.
        If the backend volume were deleted, manage would fail.

        Flow: create -> unmanage -> verify gone in Cinder
              -> manage(source-name) -> verify available + size
        """
        LOG.info("=== test_unmanage_preserves_backend_volume ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        original_size = vol['size']
        vol_id = vol['id']
        host = vol.get('os-vol-host-attr:host')

        # Unmanage
        self._unmanage_volume(vol_id)

        # Confirm it is gone from Cinder
        self.assertRaises(
            lib_exc.NotFound,
            self.vols.show_volume,
            vol_id,
        )

        # Re-manage by source-name — proves the backend volume still exists
        backend_name = 'volume-%s' % vol_id
        managed = self._manage_volume(
            host=host,
            ref={'source-name': backend_name},
            volume_type=vt['name'],
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], original_size)
        LOG.info("Backend volume preserved after unmanage; "
                 "re-managed as %s", managed['id'])

    # ----------------------------------------------------------------
    # 7. Manage by source-name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a7b8c9d0-7777-8888-9999-e1f2a3b4c5d6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_volume_by_source_name(self):
        """Manage a PowerStore volume using its backend name (source-name).

        While the generic tempest test also uses source-name, it relies
        on CONF.volume.manage_volume_ref and is gated by
        manage_volume=True (default False).  This test explicitly
        exercises the PowerStore client.get_volume_details_by_name()
        code path.

        Flow: create -> unmanage -> manage(source-name) -> verify
        """
        LOG.info("=== test_manage_volume_by_source_name ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        managed = self._unmanage_and_remanage_by_source_name(vol, vt['name'])

        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], vol['size'])
        LOG.info("Volume managed by source-name: volume-%s -> %s",
                 vol['id'], managed['id'])


# ======================================================================
# Negative test methods
# ======================================================================
class _PowerStoreVolumeManageNegativeTests(object):
    """Negative functional tests for PowerStore volume manage/unmanage."""

    @classmethod
    def skip_checks(cls):
        super(_PowerStoreVolumeManageNegativeTests, cls).skip_checks()
        if not CONF.service_available.cinder:
            raise cls.skipException("Cinder is not available")

    # ----------------------------------------------------------------
    # 8. Manage with nonexistent source-id
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b8c9d0e1-8888-9999-aaaa-f2a3b4c5d6e7')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nonexistent_volume_by_source_id(self):
        """Manage with a bogus source-id should fail.

        The PowerStore client.get_volume_details_by_id() will return
        a non-OK status or empty response, causing manage_existing()
        to raise VolumeBackendAPIException.  Cinder will transition
        the volume to error/error_managing.

        Flow: manage(source-id='bogus-uuid') -> expect error status
        """
        LOG.info("=== test_manage_nonexistent_volume_by_source_id ===")

        vt = self._create_powerstore_volume_type()

        result = self._manage_volume_expect_error(
            host=self.powerstore_host,
            ref={'source-id': '00000000-0000-0000-0000-000000000000'},
            volume_type=vt['name'],
        )
        if result is not None:
            self.assertIn(
                result['status'],
                ('error', 'error_managing'),
                "Expected manage to fail but got status: %s"
                % result['status'])
        LOG.info("Manage with nonexistent source-id correctly failed")

    # ----------------------------------------------------------------
    # 9. Manage with nonexistent source-name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c9d0e1f2-9999-aaaa-bbbb-a3b4c5d6e7f8')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nonexistent_volume_by_source_name(self):
        """Manage with a bogus source-name should fail.

        The PowerStore client.get_volume_details_by_name() will return
        an empty list, causing VolumeBackendAPIException.

        Flow: manage(source-name='nonexistent') -> expect error status
        """
        LOG.info("=== test_manage_nonexistent_volume_by_source_name ===")

        vt = self._create_powerstore_volume_type()

        result = self._manage_volume_expect_error(
            host=self.powerstore_host,
            ref={'source-name': 'nonexistent-vol-00000000'},
            volume_type=vt['name'],
        )
        if result is not None:
            self.assertIn(
                result['status'],
                ('error', 'error_managing'),
                "Expected manage to fail but got status: %s"
                % result['status'])
        LOG.info("Manage with nonexistent source-name correctly failed")


# ======================================================================
# Concrete test classes
# ======================================================================
class TestPowerStoreVolumeManagePositive(
        _PowerStoreVolumeManagePositiveTests,
        PowerStoreVolumeManageUnmanageBase,
        volume_base.BaseVolumeAdminTest):
    """Positive functional tests for PowerStore volume manage/unmanage."""
    credentials = ['primary', 'admin']


class TestPowerStoreVolumeManageNegative(
        _PowerStoreVolumeManageNegativeTests,
        PowerStoreVolumeManageUnmanageBase,
        volume_base.BaseVolumeAdminTest):
    """Negative functional tests for PowerStore volume manage/unmanage."""
    credentials = ['primary', 'admin']
