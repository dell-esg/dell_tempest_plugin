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

These tests exercise the real Cinder API calls that propagate to the
PowerStore backend for snapshot manage/unmanage operations.

The driver methods under test are:
  - driver.py:  manage_existing_snapshot(), manage_existing_snapshot_get_size(),
                unmanage_snapshot()
  - adapter.py: manage_existing_snapshot(), manage_existing_snapshot_get_size()
  - client.py:  get_snapshot_details_by_id(), get_snapshot_details_by_name()

This file covers PowerStore-specific behaviour:

  * Manage snapshot by source-id  (PowerStore snapshot UUID)
  * Manage snapshot by source-name
  * Manage snapshot preserves size
  * Delete a managed snapshot
  * Full manage -> delete lifecycle
  * Unmanage preserves backend snapshot  (re-manage proves no data loss)
  * Negative: manage with nonexistent source-id
  * Negative: manage with nonexistent source-name
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
class PowerStoreSnapshotManageUnmanageBase(object):
    """Mixin providing helpers for PowerStore snapshot manage/unmanage tests.

    All heavy lifting (volume type creation, volume/snapshot creation,
    manage, unmanage, wait-loops, cleanup) lives here so the test
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

    def _get_admin_snapshots_client(self):
        """Return the admin snapshots client."""
        client = getattr(self, 'admin_snapshots_client', None)
        if client:
            return client
        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (getattr(os_admin, 'snapshots_client_latest', None) or
                      getattr(os_admin, 'snapshots_client', None))
            if client:
                return client
        self.skipTest("Admin snapshots client not found.")

    def _get_admin_snapshot_manage_client(self):
        """Return the admin snapshot manage client."""
        client = getattr(self, 'admin_snapshot_manage_client', None)
        if client:
            return client
        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (
                getattr(os_admin, 'snapshot_manage_client_latest', None) or
                getattr(os_admin, 'snapshot_manage_client', None))
            if client:
                return client
        self.skipTest("Admin snapshot manage client not found.")

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
        super(PowerStoreSnapshotManageUnmanageBase, self).setUp()
        self.vols = self._get_admin_volumes_client()
        self.snaps = self._get_admin_snapshots_client()
        self.snap_manage = self._get_admin_snapshot_manage_client()
        self.vtypes = self._get_admin_volume_types_client()
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
                    self.powerstore_backend_name = backend_name
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
            name='ps-snap-manage-type')
        specs = {'volume_backend_name': getattr(
            self, 'powerstore_backend_name', 'powerstore')}
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
                name='ps-snap-manage-vol'),
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

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def _create_snapshot(self, volume_id):
        """Create a snapshot and wait until available."""
        snap = self.snaps.create_snapshot(
            volume_id=volume_id,
            name=data_utils.rand_name(
                prefix=CONF.resource_name_prefix,
                name='ps-snap-manage-snap'),
        )['snapshot']
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        waiters.wait_for_volume_resource_status(
            self.snaps, snap['id'], 'available')
        snap_info = self.snaps.show_snapshot(snap['id'])['snapshot']
        LOG.info("Snapshot %s available for volume %s",
                 snap_info['id'], volume_id)
        return snap_info

    def _delete_snapshot_safe(self, snap_id):
        """Delete a snapshot; ignore NotFound."""
        try:
            self.snaps.delete_snapshot(snap_id)
        except lib_exc.NotFound:
            return
        except Exception as e:
            LOG.debug("delete_snapshot(%s) raised: %s", snap_id, e)
        try:
            self.snaps.wait_for_resource_deletion(snap_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            self._wait_for_snapshot_deletion(snap_id)

    def _wait_for_snapshot_deletion(self, snap_id,
                                    timeout=VOLUME_BUILD_TIMEOUT,
                                    interval=VOLUME_BUILD_INTERVAL):
        """Poll until a snapshot is gone."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.snaps.show_snapshot(snap_id)
            except lib_exc.NotFound:
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for snapshot %s deletion", snap_id)

    def _wait_for_snapshot_status(self, snap_id, target,
                                  timeout=VOLUME_BUILD_TIMEOUT,
                                  interval=VOLUME_BUILD_INTERVAL):
        """Poll until the snapshot reaches the target status."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            s = self.snaps.show_snapshot(snap_id)['snapshot']
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
    # Unmanage / Manage snapshot helpers
    # ------------------------------------------------------------------
    def _unmanage_snapshot(self, snap_id):
        """Unmanage a snapshot and wait until it disappears from Cinder."""
        self.snaps.unmanage_snapshot(snap_id)
        LOG.info("Unmanaged snapshot %s", snap_id)
        try:
            self.snaps.wait_for_resource_deletion(snap_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            self._wait_for_snapshot_deletion(snap_id)

    def _manage_snapshot(self, volume_id, ref, name=None):
        """Manage an existing backend snapshot into Cinder.

        :param volume_id: parent volume ID in Cinder.
        :param ref: dict, e.g. {'source-id': '<ps-uuid>'} or
                    {'source-name': '<snap-name>'}.
        :param name: optional Cinder snapshot name.
        :returns: managed snapshot dict after it reaches 'available'.
        """
        name = name or data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-managed-snap')
        body = {
            'name': name,
            'volume_id': volume_id,
            'ref': ref,
        }
        new_snap = self.snap_manage.manage_snapshot(**body)['snapshot']
        LOG.info("Manage snapshot request submitted: id=%s, ref=%s",
                 new_snap['id'], ref)
        self.addCleanup(self._delete_snapshot_safe, new_snap['id'])
        waiters.wait_for_volume_resource_status(
            self.snaps, new_snap['id'], 'available')
        managed = self.snaps.show_snapshot(new_snap['id'])['snapshot']
        LOG.info("Managed snapshot %s is available", managed['id'])
        return managed

    def _manage_snapshot_expect_error(self, volume_id, ref, name=None):
        """Manage a snapshot and expect it to end up in error state.

        :returns: snapshot dict with error status, or None if the API
                  rejected the request outright.
        """
        name = name or data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-snap-manage-fail')
        body = {
            'name': name,
            'volume_id': volume_id,
            'ref': ref,
        }
        try:
            new_snap = self.snap_manage.manage_snapshot(**body)['snapshot']
        except (lib_exc.BadRequest, lib_exc.ServerFault) as e:
            LOG.info("Manage snapshot correctly rejected by API: %s", e)
            return None

        self.addCleanup(self._delete_snapshot_safe, new_snap['id'])
        # Wait for the manage operation to settle
        deadline = time.time() + VOLUME_BUILD_TIMEOUT
        while time.time() < deadline:
            s = self.snaps.show_snapshot(new_snap['id'])['snapshot']
            status = (s.get('status') or '').lower()
            if status in ('error', 'error_managing'):
                LOG.info("Manage snapshot correctly failed with status=%s",
                         status)
                return s
            if status == 'available':
                # Manage unexpectedly succeeded
                return s
            time.sleep(VOLUME_BUILD_INTERVAL)
        return self.snaps.show_snapshot(new_snap['id'])['snapshot']

    # ------------------------------------------------------------------
    # PowerStore REST API helpers
    # ------------------------------------------------------------------
    def _get_powerstore_credentials(self):
        """Read PowerStore credentials from cinder.conf.

        Searches multiple sections in priority order:
        1. The discovered backend section (e.g. powerstore2)
        2. Common section names: powerstore, powerstore1, powerstore2
        3. backend_defaults (shared config for all backends)
        """
        try:
            conf = configparser.ConfigParser()
            conf.read('/etc/cinder/cinder.conf')
            # Build candidate section list
            candidates = []
            backend_name = getattr(self, 'powerstore_backend_name', None)
            if backend_name:
                candidates.append(backend_name)
            candidates.extend(['powerstore', 'powerstore1', 'powerstore2',
                                'backend_defaults'])
            for section in candidates:
                try:
                    ps_ip = conf.get(section, 'san_ip')
                    ps_user = conf.get(section, 'san_login')
                    ps_pass = conf.get(section, 'san_password')
                    LOG.info("PowerStore credentials found in [%s]", section)
                    return ps_ip, ps_user, ps_pass
                except (configparser.NoSectionError,
                        configparser.NoOptionError):
                    continue
            LOG.warning("No PowerStore credentials found in cinder.conf")
        except Exception as e:
            LOG.warning("Cannot read PowerStore creds from cinder.conf: %s", e)
        return None, None, None

    def _get_powerstore_volume_id_by_name(self, backend_name):
        """Query the PowerStore REST API to get a volume UUID by name."""
        ps_ip, ps_user, ps_pass = self._get_powerstore_credentials()
        if not ps_ip:
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
            LOG.warning("PowerStore lookup for volume '%s' returned %s",
                        backend_name, resp.status_code)
        except Exception as e:
            LOG.warning("PowerStore REST query failed: %s", e)
        return None

    def _get_powerstore_snapshot_id_by_name(self, snap_name, parent_vol_id):
        """Query the PowerStore REST API to get a snapshot UUID by name.

        :param snap_name: snapshot name on PowerStore.
        :param parent_vol_id: PowerStore parent volume UUID.
        :returns: PowerStore snapshot UUID or None.
        """
        ps_ip, ps_user, ps_pass = self._get_powerstore_credentials()
        if not ps_ip:
            return None

        url = 'https://%s/api/rest/volume' % ps_ip
        params = {
            'name': 'eq.%s' % snap_name,
            'type': 'eq.Snapshot',
            'protection_data->>parent_id': 'eq.%s' % parent_vol_id,
            'select': 'id,name,type,protection_data',
        }
        try:
            resp = requests.get(
                url, params=params,
                auth=(ps_user, ps_pass),
                verify=False, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    ps_snap_id = data[0]['id']
                    LOG.info("PowerStore snapshot '%s' has id '%s'",
                             snap_name, ps_snap_id)
                    return ps_snap_id
            LOG.warning("PowerStore snapshot lookup for '%s' returned %s",
                        snap_name, resp.status_code)
        except Exception as e:
            LOG.warning("PowerStore REST query for snapshot failed: %s", e)
        return None

    def _get_powerstore_snapshots_for_volume(self, parent_vol_id):
        """Query PowerStore REST API for all snapshots of a volume.

        :param parent_vol_id: PowerStore parent volume UUID.
        :returns: list of snapshot dicts, or empty list.
        """
        ps_ip, ps_user, ps_pass = self._get_powerstore_credentials()
        if not ps_ip:
            return []

        url = 'https://%s/api/rest/volume' % ps_ip
        params = {
            'type': 'eq.Snapshot',
            'protection_data->>parent_id': 'eq.%s' % parent_vol_id,
            'select': 'id,name,size,type,state,protection_data',
        }
        try:
            resp = requests.get(
                url, params=params,
                auth=(ps_user, ps_pass),
                verify=False, timeout=30)
            if resp.status_code == 200:
                return resp.json() or []
            LOG.warning("PowerStore snapshots query returned %s",
                        resp.status_code)
        except Exception as e:
            LOG.warning("PowerStore REST query for snapshots failed: %s", e)
        return []

    def _powerstore_snapshot_exists(self, snapshot_id):
        """Check if a snapshot still exists on PowerStore by its UUID."""
        ps_ip, ps_user, ps_pass = self._get_powerstore_credentials()
        if not ps_ip:
            return None

        url = 'https://%s/api/rest/volume/%s' % (ps_ip, snapshot_id)
        params = {'select': 'id,name,type'}
        try:
            resp = requests.get(
                url, params=params,
                auth=(ps_user, ps_pass),
                verify=False, timeout=30)
            if resp.status_code == 200:
                return True
            elif resp.status_code == 404:
                return False
            LOG.warning("PowerStore snapshot existence check returned %s",
                        resp.status_code)
        except Exception as e:
            LOG.warning("PowerStore REST query failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Unmanage + re-manage round-trip helpers
    # ------------------------------------------------------------------
    def _unmanage_and_remanage_by_source_id(self, snap_info, volume_id,
                                            ps_parent_vol_id):
        """Unmanage a snapshot and manage it back using source-id.

        Queries the PowerStore REST API to obtain the backend snapshot UUID.

        :param snap_info: original snapshot dict.
        :param volume_id: Cinder parent volume ID.
        :param ps_parent_vol_id: PowerStore parent volume UUID.
        :returns: newly managed snapshot dict.
        """
        # Cinder's snapshot.name = 'snapshot-<uuid>' (from snapshot_name_template)
        snap_backend_name = 'snapshot-%s' % snap_info['id']
        ps_snap_id = self._get_powerstore_snapshot_id_by_name(
            snap_backend_name, ps_parent_vol_id)
        self.assertIsNotNone(
            ps_snap_id,
            "Could not find PowerStore snapshot UUID for '%s'"
            % snap_backend_name)

        self._unmanage_snapshot(snap_info['id'])

        managed = self._manage_snapshot(
            volume_id=volume_id,
            ref={'source-id': ps_snap_id},
        )
        return managed

    def _unmanage_and_remanage_by_source_name(self, snap_info, volume_id,
                                              ps_parent_vol_id):
        """Unmanage a snapshot and manage it back using source-name.

        :param snap_info: original snapshot dict.
        :param volume_id: Cinder parent volume ID.
        :param ps_parent_vol_id: PowerStore parent volume UUID.
        :returns: newly managed snapshot dict.
        """
        # Cinder's snapshot.name = 'snapshot-<uuid>' (from snapshot_name_template)
        snap_backend_name = 'snapshot-%s' % snap_info['id']

        self._unmanage_snapshot(snap_info['id'])

        managed = self._manage_snapshot(
            volume_id=volume_id,
            ref={'source-name': snap_backend_name},
        )
        return managed

    # ------------------------------------------------------------------
    # Common setup for test methods
    # ------------------------------------------------------------------
    def _setup_volume_and_snapshot(self, size=1):
        """Create a volume, a snapshot, and resolve PowerStore parent UUID.

        :returns: (vol_info, snap_info, ps_parent_vol_id)
        """
        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=size)

        snap = self._create_snapshot(vol['id'])

        # Get the PowerStore volume UUID for the parent volume
        backend_vol_name = 'volume-%s' % vol['id']
        ps_parent_vol_id = self._get_powerstore_volume_id_by_name(
            backend_vol_name)
        self.assertIsNotNone(
            ps_parent_vol_id,
            "Could not find PowerStore volume UUID for '%s'"
            % backend_vol_name)

        return vt, vol, snap, ps_parent_vol_id


# ======================================================================
# Positive test methods
# ======================================================================
class _PowerStoreSnapshotManagePositiveTests(object):
    """Positive functional tests for PowerStore snapshot manage/unmanage.

    These tests exercise real Cinder API calls that propagate to the
    PowerStore backend.  Each test targets a specific PowerStore-specific
    code path for snapshot manage/unmanage.
    """

    @classmethod
    def skip_checks(cls):
        super(_PowerStoreSnapshotManagePositiveTests, cls).skip_checks()
        if not CONF.service_available.cinder:
            raise cls.skipException("Cinder is not available")

    # ----------------------------------------------------------------
    # 1. Manage snapshot by source-id (PowerStore backend UUID)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1a2b3c4-1001-2002-3003-a4b5c6d7e8f9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_snapshot_by_source_id(self):
        """Manage a PowerStore snapshot using its backend UUID (source-id).

        Validates the source-id path in PowerStore's
        manage_existing_snapshot() which calls
        client.get_snapshot_details_by_id().

        Flow: create volume -> create snapshot -> query PowerStore for
              snapshot UUID -> unmanage -> manage(source-id) -> verify
        PowerStore API hit: GET /volume/<snapshot-uuid>
        """
        LOG.info("=== test_manage_snapshot_by_source_id ===")

        vt, vol, snap, ps_parent_vol_id = self._setup_volume_and_snapshot()

        managed = self._unmanage_and_remanage_by_source_id(
            snap, vol['id'], ps_parent_vol_id)

        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], snap['size'])
        self.assertEqual(managed['volume_id'], vol['id'])
        LOG.info("Snapshot managed by source-id: snap %s -> managed %s",
                 snap['id'], managed['id'])

    # ----------------------------------------------------------------
    # 2. Manage snapshot by source-name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d2b3c4d5-2002-3003-4004-b5c6d7e8f9a0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_snapshot_by_source_name(self):
        """Manage a PowerStore snapshot using its backend name (source-name).

        Validates the source-name path in PowerStore's
        manage_existing_snapshot() which calls
        client.get_snapshot_details_by_name().

        Flow: create volume -> create snapshot -> unmanage
              -> manage(source-name) -> verify
        PowerStore API hit: GET /volume?name=eq.<name>&type=eq.Snapshot&...
        """
        LOG.info("=== test_manage_snapshot_by_source_name ===")

        vt, vol, snap, ps_parent_vol_id = self._setup_volume_and_snapshot()

        managed = self._unmanage_and_remanage_by_source_name(
            snap, vol['id'], ps_parent_vol_id)

        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], snap['size'])
        self.assertEqual(managed['volume_id'], vol['id'])
        LOG.info("Snapshot managed by source-name: snap %s -> managed %s",
                 snap['id'], managed['id'])

    # ----------------------------------------------------------------
    # 3. Manage snapshot by source-id preserves size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d3c4d5e6-3003-4004-5005-c6d7e8f9a0b1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_snapshot_by_source_id_preserves_size(self):
        """After manage by source-id, snapshot size must match the original.

        PowerStore's manage_existing_snapshot_get_size() calls
        client.get_snapshot_details_by_id() and rounds up to GiB.
        Verify Cinder reports the correct size.

        Flow: create volume(1G) -> create snapshot -> unmanage
              -> manage(source-id) -> verify size
        """
        LOG.info("=== test_manage_snapshot_by_source_id_preserves_size ===")

        vt, vol, snap, ps_parent_vol_id = self._setup_volume_and_snapshot()
        original_size = snap['size']

        managed = self._unmanage_and_remanage_by_source_id(
            snap, vol['id'], ps_parent_vol_id)

        self.assertEqual(managed['size'], original_size,
                         "Size must be preserved after manage by source-id")
        LOG.info("Snapshot size preserved: %dG", original_size)

    # ----------------------------------------------------------------
    # 4. Delete a managed snapshot
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d4d5e6f7-4004-5005-6006-d7e8f9a0b1c2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_managed_snapshot(self):
        """Delete a snapshot that was managed back into Cinder.

        Validates that PowerStore correctly cleans up a managed snapshot
        (the backend snapshot should be removed).

        Flow: create volume -> create snapshot -> unmanage
              -> manage(source-name) -> delete -> verify gone
        """
        LOG.info("=== test_delete_managed_snapshot ===")

        vt, vol, snap, ps_parent_vol_id = self._setup_volume_and_snapshot()

        managed = self._unmanage_and_remanage_by_source_name(
            snap, vol['id'], ps_parent_vol_id)
        managed_id = managed['id']

        self.snaps.delete_snapshot(managed_id)
        self.snaps.wait_for_resource_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.snaps.show_snapshot,
            managed_id,
        )
        LOG.info("Managed snapshot %s deleted successfully", managed_id)

    # ----------------------------------------------------------------
    # 5. Full manage -> delete lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d5e6f7a8-5005-6006-7007-e8f9a0b1c2d3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_delete_snapshot_lifecycle(self):
        """Full lifecycle: create -> unmanage -> manage -> delete.

        Exercises the complete PowerStore snapshot manage/unmanage flow
        that a real operator would perform.

        Flow: create vol -> create snap -> unmanage snap
              -> manage(source-name) -> delete -> verify gone
        """
        LOG.info("=== test_manage_delete_snapshot_lifecycle ===")

        vt, vol, snap, ps_parent_vol_id = self._setup_volume_and_snapshot()

        managed = self._unmanage_and_remanage_by_source_name(
            snap, vol['id'], ps_parent_vol_id)
        managed_id = managed['id']

        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['volume_id'], vol['id'])

        self.snaps.delete_snapshot(managed_id)
        self.snaps.wait_for_resource_deletion(managed_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.snaps.show_snapshot,
            managed_id,
        )
        LOG.info("Full snapshot manage -> delete lifecycle completed "
                 "for snapshot %s", managed_id)

    # ----------------------------------------------------------------
    # 6. Unmanage preserves backend snapshot (re-manage proves it)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d6f7a8b9-6006-7007-8008-f9a0b1c2d3e4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_unmanage_preserves_backend_snapshot(self):
        """Unmanage removes Cinder metadata but leaves backend intact.

        Prove this by unmanaging and then re-managing the same snapshot.
        If the backend snapshot were deleted, manage would fail.

        Flow: create vol -> create snap -> unmanage snap
              -> verify gone in Cinder
              -> manage(source-name) -> verify available + size
        """
        LOG.info("=== test_unmanage_preserves_backend_snapshot ===")

        vt, vol, snap, ps_parent_vol_id = self._setup_volume_and_snapshot()
        original_size = snap['size']
        snap_id = snap['id']
        # Cinder's snapshot.name = 'snapshot-<uuid>' (from snapshot_name_template)
        snap_backend_name = 'snapshot-%s' % snap_id

        # Unmanage
        self._unmanage_snapshot(snap_id)

        # Confirm it is gone from Cinder
        self.assertRaises(
            lib_exc.NotFound,
            self.snaps.show_snapshot,
            snap_id,
        )

        # Re-manage by source-name — proves the backend snapshot exists
        managed = self._manage_snapshot(
            volume_id=vol['id'],
            ref={'source-name': snap_backend_name},
        )
        self.assertEqual(managed['status'], 'available')
        self.assertEqual(managed['size'], original_size)
        self.assertEqual(managed['volume_id'], vol['id'])
        LOG.info("Backend snapshot preserved after unmanage; "
                 "re-managed as %s", managed['id'])

    # ----------------------------------------------------------------
    # 7. Unmanage + verify backend snapshot still exists on PowerStore
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d7a8b9c0-7007-8008-9009-a0b1c2d3e4f5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_unmanage_snapshot_backend_exists(self):
        """After unmanage, verify the snapshot still exists on PowerStore.

        Directly queries the PowerStore REST API to confirm the snapshot
        is still present after Cinder unmanage.

        Flow: create vol -> create snap -> get PS snap UUID
              -> unmanage -> query PS REST -> verify exists
        """
        LOG.info("=== test_unmanage_snapshot_backend_exists ===")

        vt, vol, snap, ps_parent_vol_id = self._setup_volume_and_snapshot()
        # Cinder's snapshot.name = 'snapshot-<uuid>' (from snapshot_name_template)
        snap_backend_name = 'snapshot-%s' % snap['id']

        ps_snap_id = self._get_powerstore_snapshot_id_by_name(
            snap_backend_name, ps_parent_vol_id)
        self.assertIsNotNone(
            ps_snap_id,
            "Could not find PowerStore snapshot UUID for '%s'"
            % snap_backend_name)

        # Unmanage
        self._unmanage_snapshot(snap['id'])

        # Verify on PowerStore the snapshot still exists
        exists = self._powerstore_snapshot_exists(ps_snap_id)
        self.assertTrue(
            exists,
            "PowerStore snapshot %s should still exist after unmanage"
            % ps_snap_id)
        LOG.info("PowerStore snapshot %s still exists after unmanage",
                 ps_snap_id)

        # Cleanup: re-manage and then let cleanup delete it
        self._manage_snapshot(
            volume_id=vol['id'],
            ref={'source-id': ps_snap_id},
        )


# ======================================================================
# Negative test methods
# ======================================================================
class _PowerStoreSnapshotManageNegativeTests(object):
    """Negative functional tests for PowerStore snapshot manage/unmanage."""

    @classmethod
    def skip_checks(cls):
        super(_PowerStoreSnapshotManageNegativeTests, cls).skip_checks()
        if not CONF.service_available.cinder:
            raise cls.skipException("Cinder is not available")

    # ----------------------------------------------------------------
    # 8. Manage with nonexistent source-id
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d8b9c0d1-8008-9009-a00a-b1c2d3e4f5a6')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nonexistent_snapshot_by_source_id(self):
        """Manage with a bogus source-id should fail.

        The PowerStore client.get_snapshot_details_by_id() will return
        a non-OK status or empty response, causing manage_existing_snapshot()
        to raise VolumeBackendAPIException.  Cinder will transition
        the snapshot to error/error_managing.

        Flow: create vol -> manage snapshot(source-id='bogus-uuid')
              -> expect error status
        """
        LOG.info("=== test_manage_nonexistent_snapshot_by_source_id ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        result = self._manage_snapshot_expect_error(
            volume_id=vol['id'],
            ref={'source-id': '00000000-0000-0000-0000-000000000000'},
        )
        if result is not None:
            self.assertIn(
                result['status'],
                ('error', 'error_managing'),
                "Expected manage to fail but got status: %s"
                % result['status'])
        LOG.info("Manage snapshot with nonexistent source-id correctly failed")

    # ----------------------------------------------------------------
    # 9. Manage with nonexistent source-name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d9c0d1e2-9009-a00a-b00b-c2d3e4f5a6b7')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nonexistent_snapshot_by_source_name(self):
        """Manage with a bogus source-name should fail.

        The PowerStore client.get_snapshot_details_by_name() will return
        an empty list, causing VolumeBackendAPIException.

        Flow: create vol -> manage snapshot(source-name='nonexistent')
              -> expect error status
        """
        LOG.info("=== test_manage_nonexistent_snapshot_by_source_name ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        result = self._manage_snapshot_expect_error(
            volume_id=vol['id'],
            ref={'source-name': 'nonexistent-snap-00000000'},
        )
        if result is not None:
            self.assertIn(
                result['status'],
                ('error', 'error_managing'),
                "Expected manage to fail but got status: %s"
                % result['status'])
        LOG.info("Manage snapshot with nonexistent source-name "
                 "correctly failed")


# ======================================================================
# Concrete test classes
# ======================================================================
class TestPowerStoreSnapshotManagePositive(
        _PowerStoreSnapshotManagePositiveTests,
        PowerStoreSnapshotManageUnmanageBase,
        volume_base.BaseVolumeAdminTest):
    """Positive functional tests for PowerStore snapshot manage/unmanage."""
    credentials = ['primary', 'admin']


class TestPowerStoreSnapshotManageNegative(
        _PowerStoreSnapshotManageNegativeTests,
        PowerStoreSnapshotManageUnmanageBase,
        volume_base.BaseVolumeAdminTest):
    """Negative functional tests for PowerStore snapshot manage/unmanage."""
    credentials = ['primary', 'admin']
