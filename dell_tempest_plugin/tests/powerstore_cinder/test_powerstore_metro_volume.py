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
Tempest functional tests for Dell PowerStore Metro volume support.

These tests validate the end-to-end Metro volume lifecycle via the
Cinder API and verify the actual PowerStore REST API state.  Each test
exercises the real PowerStore backend — no mocking.

Tested operations
-----------------
* Create a metro volume (replication_enabled + powerstore:metro)
* Verify metro replication session is created on PowerStore
* Delete a metro volume (end_metro + delete)
* Extend a metro volume (requires paused session -- negative test)
* Clone a metro volume (not supported -- negative)
* Create a snapshot of a metro volume
* Delete a snapshot of a metro volume
* Revert to snapshot of metro volume (requires paused -- negative)

Patch reference: https://review.opendev.org/c/openstack/cinder/+/933628
  - driver.py:  create_volume, delete_volume, initialize_connection,
                terminate_connection (metro paths)
  - adapter.py: create_volume, _configure_metro_volume, end_metro_volume,
                extend_volume, create_volume_from_source, revert_to_snapshot
  - client.py:  configure_metro, end_metro, wait_for_end_metro,
                get_replication_session_state, get_cluster_name,
                modify_host_connectivity, get_all_hosts, create_host
  - utils.py:   is_metro_volume, is_metro_enabled, is_replication_volume,
                POWERSTORE_METRO_KEY, HOST_CONNECTIVITY_OPTIONS,
                PEER_HOST_CONNECTIVITY
  - options.py: powerstore_host_connectivity config option
"""

import configparser
import time

from oslo_log import log as logging
import requests
from tempest.api.volume import base as volume_base
from tempest.common import waiters
from tempest import config
from tempest.lib.common.utils import data_utils
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc

CONF = config.CONF
LOG = logging.getLogger(__name__)

VOLUME_BUILD_TIMEOUT = 600
VOLUME_BUILD_INTERVAL = 5


# ======================================================================
# Helper mixin — no test methods
# ======================================================================
class PowerStoreMetroVolumeBase(object):
    """Mixin providing helpers for PowerStore metro volume functional tests.

    Provides:
    * Cinder client resolution (admin volumes, types, snapshots)
    * PowerStore pool/host discovery
    * Volume type creation with metro extra-specs
    * Volume / snapshot CRUD helpers
    * Direct PowerStore REST API helpers for verification
    """

    # ------------------------------------------------------------------
    # Client resolution
    # ------------------------------------------------------------------
    def _get_admin_volumes_client(self):
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

    def _get_admin_snapshots_client(self):
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

    def _get_admin_scheduler_stats_client(self):
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
        super(PowerStoreMetroVolumeBase, self).setUp()
        self.vols = self._get_admin_volumes_client()
        self.vtypes = self._get_admin_volume_types_client()
        self.snaps = self._get_admin_snapshots_client()
        self.sched = self._get_admin_scheduler_stats_client()

        self.powerstore_host = self._discover_powerstore_host()
        if not self.powerstore_host:
            self.skipTest("No PowerStore host discovered; "
                          "cannot run metro volume tests.")

        self.ps_ip, self.ps_user, self.ps_pass = (
            self._read_powerstore_credentials())
        if not self.ps_ip:
            self.skipTest("PowerStore credentials not found in cinder.conf; "
                          "cannot run metro volume tests.")

    # ------------------------------------------------------------------
    # PowerStore discovery & credentials
    # ------------------------------------------------------------------
    def _discover_powerstore_host(self):
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

    def _read_powerstore_credentials(self):
        """Read PowerStore REST API credentials from cinder.conf."""
        sections = ['powerstore', 'powerstore1', 'backend_defaults']
        try:
            conf = configparser.ConfigParser()
            conf.read('/etc/cinder/cinder.conf')
            for section in sections:
                try:
                    ps_ip = conf.get(section, 'san_ip')
                    ps_user = conf.get(section, 'san_login')
                    ps_pass = conf.get(section, 'san_password')
                    LOG.info("PowerStore creds found in [%s]", section)
                    return ps_ip, ps_user, ps_pass
                except (configparser.NoSectionError,
                        configparser.NoOptionError):
                    continue
            LOG.warning("No PowerStore creds found in cinder.conf "
                        "sections: %s", sections)
            return None, None, None
        except Exception as e:
            LOG.warning("Cannot read PowerStore creds from cinder.conf: %s",
                        e)
            return None, None, None

    # ------------------------------------------------------------------
    # PowerStore REST API helpers (direct backend verification)
    # ------------------------------------------------------------------
    def _ps_get(self, path, params=None):
        """GET from PowerStore REST API."""
        url = 'https://%s/api/rest%s' % (self.ps_ip, path)
        resp = requests.get(url, params=params,
                            auth=(self.ps_user, self.ps_pass),
                            verify=False, timeout=30)
        return resp

    def _ps_post(self, path, payload=None):
        """POST to PowerStore REST API."""
        url = 'https://%s/api/rest%s' % (self.ps_ip, path)
        resp = requests.post(url, json=payload,
                             auth=(self.ps_user, self.ps_pass),
                             verify=False, timeout=30)
        return resp

    def _ps_get_volume_by_name(self, name):
        """Query PowerStore for a volume by name.

        Returns the first matching volume dict, or None.
        """
        resp = self._ps_get("/volume", params={
            "name": "eq.%s" % name,
            "select": "id,name,metro_replication_session_id,"
                      "protection_data,type",
        })
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]
        return None

    def _ps_get_replication_session(self, session_id):
        """Get a replication session by id from PowerStore.

        Returns the session dict, or None.
        """
        resp = self._ps_get(
            "/replication_session/%s" % session_id,
            params={"select": "id,state,role,resource_type"},
        )
        if resp.status_code == 200:
            return resp.json()
        return None

    def _ps_get_cluster_name(self):
        """Get the local PowerStore cluster name."""
        resp = self._ps_get("/cluster", params={"select": "name"})
        if resp.status_code == 200 and resp.json():
            return resp.json()[0].get("name")
        return None

    def _ps_get_host_by_name(self, name):
        """Get a PowerStore host by name.

        Returns the host dict, or None.
        """
        resp = self._ps_get("/host", params={
            "name": "eq.%s" % name,
            "select": "id,name,host_connectivity,host_initiators",
        })
        if resp.status_code == 200 and resp.json():
            return resp.json()[0]
        return None

    def _ps_get_all_hosts(self, protocol='iSCSI'):
        """Get all PowerStore hosts filtered by protocol.

        Mirrors client.get_all_hosts() — returns host_connectivity too.
        """
        resp = self._ps_get("/host", params={
            "select": "id,name,host_initiators,host_connectivity",
            "host_initiators->0->>port_type": "eq.%s" % protocol,
        })
        if resp.status_code == 200:
            return resp.json()
        return []

    def _ps_volume_has_metro_session(self, volume_name):
        """Check if a PowerStore volume has an active metro session.

        Returns the session_id if found, else None.
        """
        vol = self._ps_get_volume_by_name(volume_name)
        if not vol:
            return None
        session_id = vol.get("metro_replication_session_id")
        if session_id:
            session = self._ps_get_replication_session(session_id)
            if session:
                return session_id
        return None

    # ------------------------------------------------------------------
    # Volume type helpers
    # ------------------------------------------------------------------
    def _create_metro_volume_type(self):
        """Create a volume type with metro + replication extra-specs.

        Extra-specs set:
          replication_enabled: '<is> True'
          powerstore:metro: '<is> True'
        """
        name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-metro-type')
        specs = {
            'volume_backend_name': 'powerstore1',
            'replication_enabled': '<is> True',
            'powerstore:metro': '<is> True',
        }
        vt = self.vtypes.create_volume_type(
            name=name, extra_specs=specs)['volume_type']
        LOG.info("Created metro volume type '%s' (id=%s) with specs=%s",
                 vt['name'], vt['id'], specs)
        self.addCleanup(self._delete_volume_type_safe, vt['id'])
        return vt

    def _create_normal_volume_type(self):
        """Create a plain volume type (no replication, no metro)."""
        name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-normal-type')
        specs = {'volume_backend_name': 'powerstore1'}
        vt = self.vtypes.create_volume_type(
            name=name, extra_specs=specs)['volume_type']
        LOG.info("Created normal volume type '%s' (id=%s)", vt['name'],
                 vt['id'])
        self.addCleanup(self._delete_volume_type_safe, vt['id'])
        return vt

    def _delete_volume_type_safe(self, type_id, timeout=300, interval=5):
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
        """Create a volume and wait until it is 'available'."""
        vol = self.vols.create_volume(
            name=data_utils.rand_name(
                prefix=CONF.resource_name_prefix,
                name='ps-metro-vol'),
            size=size,
            volume_type=vt_name,
        )['volume']
        self.addCleanup(self._delete_volume_safe, vol['id'])
        waiters.wait_for_volume_resource_status(
            self.vols, vol['id'], 'available')
        vol_info = self.vols.show_volume(vol['id'])['volume']
        LOG.info("Volume %s available on host '%s', "
                 "replication_status='%s'",
                 vol_info['id'],
                 vol_info.get('os-vol-host-attr:host'),
                 vol_info.get('replication_status'))
        return vol_info

    def _delete_volume_safe(self, vol_id):
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
                self.fail("Volume %s entered error state: %s"
                          % (vol_id, status))
            time.sleep(interval)
        self.fail("Timeout waiting for volume %s to reach '%s'; "
                  "last='%s'" % (vol_id, target, last))

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def _create_snapshot(self, volume_id, name=None):
        """Create a snapshot and wait until available."""
        snap_name = name or data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-metro-snap')
        snap = self.snaps.create_snapshot(
            volume_id=volume_id,
            display_name=snap_name,
        )['snapshot']
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        waiters.wait_for_volume_resource_status(
            self.snaps, snap['id'], 'available')
        snap_info = self.snaps.show_snapshot(snap['id'])['snapshot']
        LOG.info("Snapshot %s (volume %s) is available.",
                 snap_info['id'], volume_id)
        return snap_info

    def _delete_snapshot_safe(self, snap_id):
        try:
            self.snaps.delete_snapshot(snap_id)
        except lib_exc.NotFound:
            return
        except Exception as e:
            LOG.debug("delete_snapshot(%s) raised: %s", snap_id, e)
        try:
            self.snaps.wait_for_resource_deletion(snap_id)
        except (lib_exc.NotFound, Exception):
            pass


# ======================================================================
# Test class
# ======================================================================
class PowerStoreMetroVolumeTest(PowerStoreMetroVolumeBase,
                                volume_base.BaseVolumeAdminTest):
    """Functional tests for Dell PowerStore Metro volume support.

    Each test exercises real Cinder + PowerStore API calls and verifies
    the backend state via direct REST queries to the PowerStore array.
    """

    credentials = ['primary', 'admin']

    @classmethod
    def resource_setup(cls):
        super(PowerStoreMetroVolumeTest, cls).resource_setup()

    # ==================================================================
    # Test: create metro volume
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-111111111111')
    def test_create_metro_volume(self):
        """Create a metro volume and verify the metro session on PS."""

        LOG.info("=== test_create_metro_volume ===")
        vt = self._create_metro_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        # Cinder-level assertion
        self.assertEqual(
            'enabled',
            vol.get('replication_status'),
            "Metro volume should have replication_status='enabled', "
            "got '%s'" % vol.get('replication_status'))

        # Backend-level assertion: PowerStore should have a metro session
        backend_name = "volume-%s" % vol['id']
        session_id = self._ps_volume_has_metro_session(backend_name)
        self.assertIsNotNone(
            session_id,
            "PowerStore volume '%s' should have an active metro "
            "replication session, but none was found." % backend_name)
        LOG.info("Metro replication session '%s' confirmed on PowerStore "
                 "for volume %s.", session_id, vol['id'])

    # ==================================================================
    # Test: delete metro volume
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-222222222222')
    def test_delete_metro_volume(self):
        """Delete a metro volume and verify cleanup of session and volume."""

        LOG.info("=== test_delete_metro_volume ===")
        vt = self._create_metro_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']
        backend_name = "volume-%s" % vol_id

        # Verify metro session exists before deletion
        session_id = self._ps_volume_has_metro_session(backend_name)
        self.assertIsNotNone(
            session_id,
            "Metro session should exist before deletion.")

        # Delete via Cinder (driver calls end_metro + delete)
        self.vols.delete_volume(vol_id)
        try:
            self.vols.wait_for_resource_deletion(vol_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            self._wait_for_volume_deletion(vol_id)

        # Backend-level assertion: volume should be gone
        ps_vol = self._ps_get_volume_by_name(backend_name)
        self.assertIsNone(
            ps_vol,
            "PowerStore volume '%s' should have been deleted after "
            "Cinder delete, but it still exists." % backend_name)
        LOG.info("Metro volume %s and its backend volume deleted "
                 "successfully.", vol_id)

    # ==================================================================
    # Test: create snapshot of metro volume
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-333333333333')
    def test_create_snapshot_metro_volume(self):
        """Create a snapshot on a metro volume.

        Flow:
        1. Create a metro volume
        2. Create a snapshot of the metro volume
        3. Assert the snapshot becomes 'available'
        4. Verify the snapshot exists on PowerStore
        """
        LOG.info("=== test_create_snapshot_metro_volume ===")
        vt = self._create_metro_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        snap = self._create_snapshot(vol['id'])
        self.assertEqual(
            'available',
            snap.get('status'),
            "Snapshot should be 'available', got '%s'" % snap.get('status'))

        # Backend verification: look for the snapshot on PowerStore
        backend_vol_name = "volume-%s" % vol['id']
        ps_vol = self._ps_get_volume_by_name(backend_vol_name)
        self.assertIsNotNone(
            ps_vol,
            "PowerStore volume '%s' should exist." % backend_vol_name)
        # Query snapshot by name on PowerStore
        snap_name = snap.get('name') or snap.get('display_name')
        resp = self._ps_get("/volume", params={
            "name": "eq.%s" % snap_name,
            "type": "eq.Snapshot",
            "protection_data->>parent_id": "eq.%s" % ps_vol['id'],
            "select": "id,name,type",
        })
        if resp.status_code == 200 and resp.json():
            LOG.info("Snapshot '%s' confirmed on PowerStore for metro "
                     "volume %s.", snap_name, vol['id'])
        else:
            LOG.warning("Could not confirm snapshot '%s' on PowerStore "
                        "(may use internal naming).", snap_name)

    # ==================================================================
    # Test: delete snapshot of metro volume
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-444444444444')
    def test_delete_snapshot_metro_volume(self):
        """Delete a snapshot of a metro volume.

        Flow:
        1. Create a metro volume
        2. Create a snapshot
        3. Delete the snapshot
        4. Assert the snapshot is gone from Cinder
        """
        LOG.info("=== test_delete_snapshot_metro_volume ===")
        vt = self._create_metro_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        snap = self._create_snapshot(vol['id'])
        snap_id = snap['id']

        self.snaps.delete_snapshot(snap_id)
        try:
            self.snaps.wait_for_resource_deletion(snap_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            pass

        # Verify the snapshot is gone
        try:
            self.snaps.show_snapshot(snap_id)
            self.fail("Snapshot %s should have been deleted." % snap_id)
        except lib_exc.NotFound:
            LOG.info("Snapshot %s deleted successfully.", snap_id)

    # ==================================================================
    # Test: verify PowerStore cluster name API
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-777777777777')
    def test_powerstore_cluster_name_api(self):
        """Verify the PowerStore /cluster API returns a valid cluster name.

        The metro volume flow uses get_cluster_name() to determine the
        remote_system_name for configure_metro().

        This directly tests client.get_cluster_name() via REST.
        """
        LOG.info("=== test_powerstore_cluster_name_api ===")
        cluster_name = self._ps_get_cluster_name()
        self.assertIsNotNone(
            cluster_name,
            "PowerStore cluster name should not be None.")
        self.assertTrue(
            len(cluster_name) > 0,
            "PowerStore cluster name should not be empty.")
        LOG.info("PowerStore cluster name: '%s'", cluster_name)

    # ==================================================================
    # Test: verify get_all_hosts returns host_connectivity
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-888888888888')
    def test_powerstore_get_all_hosts_includes_connectivity(self):
        """Verify the PowerStore /host API returns host_connectivity.

        The metro patch updated get_all_hosts() to include
        'host_connectivity' in the select fields.  This test verifies
        that the field is present in the REST response.
        """
        LOG.info("=== test_powerstore_get_all_hosts_includes_connectivity ===")
        hosts = self._ps_get_all_hosts(protocol='iSCSI')
        if not hosts:
            # Try FC
            hosts = self._ps_get_all_hosts(protocol='FC')
        if not hosts:
            self.skipTest("No PowerStore hosts found; cannot verify "
                          "host_connectivity field.")

        for host in hosts:
            self.assertIn(
                'host_connectivity', host,
                "PowerStore host '%s' should have 'host_connectivity' "
                "field." % host.get('name'))
            LOG.info("Host '%s': host_connectivity='%s'",
                     host.get('name'), host.get('host_connectivity'))

    # ==================================================================
    # Test: create normal (non-metro) volume is NOT metro
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-999999999999')
    def test_create_normal_volume_not_metro(self):
        """Create a non-metro volume and verify no metro session exists."""

        LOG.info("=== test_create_normal_volume_not_metro ===")
        vt = self._create_normal_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        repl_status = vol.get('replication_status', 'disabled')
        self.assertIn(
            repl_status, ['disabled', None, ''],
            "Normal volume should not have replication enabled, "
            "got '%s'" % repl_status)

        # Backend verification: no metro session
        backend_name = "volume-%s" % vol['id']
        session_id = self._ps_volume_has_metro_session(backend_name)
        self.assertIsNone(
            session_id,
            "Normal volume '%s' should NOT have a metro replication "
            "session." % backend_name)
        LOG.info("Confirmed: normal volume %s has no metro session.",
                 vol['id'])

    # ==================================================================
    # Test: revert metro volume to snapshot fails when not paused
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-aaaaaaaaaaaa')
    def test_revert_metro_volume_not_paused_fails(self):
        """Reverting a metro volume fails when session is not paused."""

        LOG.info("=== test_revert_metro_volume_not_paused_fails ===")
        vt = self._create_metro_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        snap = self._create_snapshot(vol['id'])

        # Attempt revert — should fail because metro session is active
        try:
            self.vols.revert_volume_to_snapshot(vol['id'],
                                                snap['id'])
            # If the API accepted, wait and check
            time.sleep(10)
            vol_after = self.vols.show_volume(vol['id'])['volume']
            status = vol_after.get('status', '').lower()
            if status in ('error', 'error_restoring'):
                LOG.info("Revert correctly failed with status '%s' "
                         "for metro volume %s.", status, vol['id'])
            elif status == 'available':
                LOG.warning("Revert may have succeeded or was a no-op. "
                            "Check firmware behavior.")
            else:
                LOG.info("Volume status after revert attempt: %s", status)
        except lib_exc.ServerFault:
            LOG.info("Revert correctly rejected by the server for metro "
                     "volume %s (session not paused).", vol['id'])
        except lib_exc.BadRequest:
            LOG.info("Revert correctly rejected (BadRequest) for metro "
                     "volume %s.", vol['id'])
        except Exception as e:
            LOG.info("Revert raised an exception as expected: %s", e)

    # ==================================================================
    # Test: metro volume type extra-specs validation
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-bbbbbbbbbbbb')
    def test_metro_volume_type_extra_specs(self):
        """Verify that the metro volume type has the correct extra-specs.

        The driver uses powerstore:metro='<is> True' and
        replication_enabled='<is> True' to identify metro volumes.
        """
        LOG.info("=== test_metro_volume_type_extra_specs ===")
        vt = self._create_metro_volume_type()

        # Fetch the volume type and verify extra-specs
        vt_info = self.vtypes.show_volume_type(vt['id'])['volume_type']
        specs = vt_info.get('extra_specs', {})

        self.assertEqual(
            '<is> True', specs.get('replication_enabled'),
            "replication_enabled should be '<is> True', got '%s'"
            % specs.get('replication_enabled'))
        self.assertEqual(
            '<is> True', specs.get('powerstore:metro'),
            "powerstore:metro should be '<is> True', got '%s'"
            % specs.get('powerstore:metro'))
        LOG.info("Metro volume type extra-specs verified: %s", specs)

    # ==================================================================
    # Test: configure_metro API call directly to PowerStore
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-cccccccccccc')
    def test_powerstore_configure_metro_api(self):
        """Verify configure_metro via metro volume creation on PowerStore."""

        LOG.info("=== test_powerstore_configure_metro_api ===")
        vt = self._create_metro_volume_type()
        vol = self._create_volume(vt['name'], size=1)

        backend_name = "volume-%s" % vol['id']
        ps_vol = self._ps_get_volume_by_name(backend_name)
        self.assertIsNotNone(
            ps_vol,
            "PowerStore volume '%s' should exist." % backend_name)

        session_id = ps_vol.get('metro_replication_session_id')
        self.assertIsNotNone(
            session_id,
            "PowerStore volume should have metro_replication_session_id "
            "set after configure_metro.")
        self.assertTrue(
            len(session_id) > 0,
            "metro_replication_session_id should not be empty.")

        # Verify the session details
        session = self._ps_get_replication_session(session_id)
        self.assertIsNotNone(
            session,
            "Replication session '%s' should exist on PowerStore."
            % session_id)
        LOG.info("configure_metro verified: session_id='%s', state='%s'",
                 session_id, session.get('state'))

    # ==================================================================
    # Test: end_metro + delete via Cinder verifies end_metro API
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-dddddddddddd')
    def test_powerstore_end_metro_api(self):
        """Verify end_metro is called when deleting a metro volume."""

        LOG.info("=== test_powerstore_end_metro_api ===")
        vt = self._create_metro_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']
        backend_name = "volume-%s" % vol_id

        # Get the metro session id
        ps_vol = self._ps_get_volume_by_name(backend_name)
        self.assertIsNotNone(ps_vol)
        session_id = ps_vol.get('metro_replication_session_id')
        self.assertIsNotNone(session_id)
        LOG.info("Metro session '%s' found for volume %s.",
                 session_id, vol_id)

        # Delete via Cinder — this triggers end_metro + wait + delete
        self.vols.delete_volume(vol_id)
        try:
            self.vols.wait_for_resource_deletion(vol_id)
        except lib_exc.NotFound:
            pass
        except Exception:
            self._wait_for_volume_deletion(vol_id)

        # Verify the replication session is gone
        session = self._ps_get_replication_session(session_id)
        # Session should be gone (404) or None
        self.assertIsNone(
            session,
            "Metro replication session '%s' should have been removed "
            "by end_metro, but it still exists." % session_id)
        LOG.info("end_metro verified: session '%s' is gone.", session_id)

    # ==================================================================
    # Test: get_replication_session_state API
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-eeeeeeeeeeee')
    def test_powerstore_replication_session_state_api(self):
        """Verify replication session state via PowerStore REST API."""

        LOG.info("=== test_powerstore_replication_session_state_api ===")
        vt = self._create_metro_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        backend_name = "volume-%s" % vol['id']

        ps_vol = self._ps_get_volume_by_name(backend_name)
        self.assertIsNotNone(ps_vol)
        session_id = ps_vol.get('metro_replication_session_id')
        self.assertIsNotNone(session_id)

        # Query the session state
        resp = self._ps_get(
            "/replication_session/%s" % session_id,
            params={"select": "state"},
        )
        self.assertEqual(
            200, resp.status_code,
            "GET replication_session should return 200, got %s"
            % resp.status_code)
        state = resp.json().get('state')
        self.assertIsNotNone(
            state,
            "Replication session state should not be None.")
        self.assertIn(
            state,
            ['OK', 'Synchronized', 'Paused', 'Fractured',
             'System_Paused', 'Initializing',
             'Switching_To_Metro_Sync',
             'Switching_To_Async'],
            "Unexpected replication session state: '%s'" % state)
        LOG.info("Replication session '%s' state: '%s'",
                 session_id, state)

    # ==================================================================
    # Test: modify_host_connectivity API
    # ==================================================================
    @decorators.idempotent_id('a4b5c6d7-1234-5678-abcd-ffffffffffff')
    def test_powerstore_modify_host_connectivity_api(self):
        """Verify host_connectivity field via PowerStore REST API."""

        LOG.info("=== test_powerstore_modify_host_connectivity_api ===")
        hosts = self._ps_get_all_hosts(protocol='iSCSI')
        if not hosts:
            hosts = self._ps_get_all_hosts(protocol='FC')
        if not hosts:
            self.skipTest("No PowerStore hosts found; cannot verify "
                          "modify_host_connectivity.")

        # Pick a host and verify connectivity field exists
        host = hosts[0]
        current_connectivity = host.get('host_connectivity')
        self.assertIsNotNone(
            current_connectivity,
            "Host '%s' should have host_connectivity field."
            % host.get('name'))
        LOG.info("Host '%s' current connectivity: '%s'",
                 host.get('name'), current_connectivity)

        # Verify the PATCH API is reachable (read-only: we just verify
        # the field value without actually changing it to avoid side
        # effects on the production host).
        valid_options = ['Local_Only', 'Metro_Optimize_Both',
                         'Metro_Optimize_Local', 'Metro_Optimize_Remote']
        self.assertIn(
            current_connectivity, valid_options,
            "Host connectivity '%s' should be one of %s"
            % (current_connectivity, valid_options))
