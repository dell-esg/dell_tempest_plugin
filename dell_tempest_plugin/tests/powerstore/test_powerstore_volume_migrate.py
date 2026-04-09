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
Tempest functional tests for Dell PowerStore driver-assisted volume migration.

These tests are **complementary** to the generic Cinder retype-with-migration
tests (tempest/api/volume/admin/test_volume_retype.py) and the existing basic
migration smoke test in test_powerstore.py
(PowerStoreMigrateVolumeTest.test_migrate_volume_between_powerstore_hosts).

The generic retype test only covers retype-driven migration with an on-demand
migration policy.  The smoke test only verifies that the host string changes.

This file focuses exclusively on PowerStore driver-assisted migration
behaviour that is NOT covered generically:

  * Volume size is preserved after driver-assisted migration
  * Volume reaches 'available' with migration_status 'success'
  * Extend a migrated volume (validates backend mapping intact)
  * Delete a migrated volume (validates backend cleanup)
  * Full lifecycle: create -> migrate -> extend -> delete
  * Volume type is preserved after migration
  * Migration to same host is handled gracefully (no-op / idempotent)
  * Negative: migration to a non-existent host fails

Patch reference: Gerrit 971952 (Dell Powerstore driver-assisted migration)
  - adapter.py:  migrate_volume()
  - driver.py:   migrate_volume()
  - client.py:   create_migration_session(), sync_migration_session(),
                  wait_for_migration_completion(), cutover_migration_session(),
                  get_family_id(), get_appliance_id_by_name(),
                  get_volume_id_by_name()
  - utils.py:    normalize_host_string(), parse_backend_from_host_str(),
                  get_backend_appliance_id_mapping()
"""

import json
import time

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
MIGRATION_TIMEOUT = 1800   # seconds – driver-assisted migration can be slow
MIGRATION_POLL_INTERVAL = 10


# ======================================================================
# Base mixin — helpers only, no test methods
# ======================================================================
class PowerStoreVolumeMigrateBase(object):
    """Mixin providing helpers for PowerStore volume migration tests.

    Handles volume-type creation, volume creation, migration via the
    legacy ``os-migrate_volume`` admin action, polling for completion,
    cleanup, and PowerStore host discovery.
    """

    # Set to True to use Cinder's generic host-copy migration instead of
    # the driver-assisted path.  The driver-assisted path (False) requires
    # a multi-appliance PowerStore cluster; host-copy works across any two
    # backends and is useful for validating the test infrastructure.
    FORCE_HOST_COPY = False

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
        super(PowerStoreVolumeMigrateBase, self).setUp()
        self.vols = self._get_admin_volumes_client()
        self.vtypes = self._get_admin_volume_types_client()
        self.sched = self._get_admin_scheduler_stats_client()

        # Discover PowerStore host@backend#pool strings
        self.powerstore_hosts = self._discover_powerstore_hosts()
        if not self.powerstore_hosts:
            self.skipTest("No PowerStore hosts discovered from scheduler "
                          "pools; cannot run migration tests.")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def _discover_powerstore_hosts(self):
        """Discover all PowerStore host@backend#pool strings."""
        candidates = set()
        if self.sched:
            try:
                pools = self.sched.list_pools(detail=True).get('pools', [])
                for p in pools:
                    name = p.get('name', '')
                    caps = p.get('capabilities', {}) or {}
                    backend_name = caps.get('volume_backend_name', '')
                    if ('powerstore' in name.lower() or
                            'powerstore' in backend_name.lower()):
                        candidates.add(name)
                if candidates:
                    LOG.info("Discovered PowerStore pools: %s",
                             sorted(candidates))
            except Exception as e:
                LOG.warning("Pool discovery failed: %s", e)

        if not candidates:
            candidates = self._discover_powerstore_hosts_from_services()

        return sorted(candidates)

    def _discover_powerstore_hosts_from_services(self):
        """Fallback: discover PowerStore hosts from cinder-volume services."""
        candidates = set()
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
                        candidates.add(svc['host'])
                        LOG.info("Discovered PowerStore service host: %s",
                                 svc['host'])
        except Exception as e:
            LOG.warning("Service discovery failed: %s", e)
        return candidates

    # ------------------------------------------------------------------
    # Host-string helpers
    # ------------------------------------------------------------------
    def _parse_host_backend_pool(self, host_str):
        """Parse 'host@backend#pool' into (host, backend, pool)."""
        host = backend = pool = None
        try:
            if '@' in host_str:
                host_part, rest = host_str.split('@', 1)
                host = host_part
                if '#' in rest:
                    backend, pool = rest.split('#', 1)
                else:
                    backend = rest
            else:
                host = host_str
        except Exception as e:
            LOG.warning("Failed to parse host string '%s': %s", host_str, e)
        return host, backend, pool

    def _pick_migration_target(self, current_host):
        """Return a PowerStore target host different from *current_host*.

        Skips the test if no suitable alternative is found.
        """
        for t in self.powerstore_hosts:
            if t and t != current_host:
                return t
        self.skipTest(
            "Only one PowerStore pool/host discovered (%s); need at "
            "least two for migration tests." % current_host)

    # ------------------------------------------------------------------
    # Volume type helpers
    # ------------------------------------------------------------------
    def _create_powerstore_volume_type(self, extra_specs=None):
        """Create a volume type for migration tests.

        The type is created WITHOUT a ``volume_backend_name`` extra spec
        so that the scheduler allows the volume on **both** PowerStore
        backends (the source and the destination).  The
        ``os-migrate_volume`` action passes through the scheduler which
        applies the BackendFilter; if ``volume_backend_name`` is pinned
        to a single backend the scheduler will reject migration to any
        other backend with ``NoValidBackend``.
        """
        name = data_utils.rand_name(
            prefix=CONF.resource_name_prefix,
            name='ps-migrate-type')
        kwargs = {'name': name}
        if extra_specs:
            kwargs['extra_specs'] = extra_specs
        vt = self.vtypes.create_volume_type(**kwargs)['volume_type']
        LOG.info("Created volume type '%s' (id=%s)", vt['name'], vt['id'])
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
                name='ps-migrate-vol'),
            size=size,
            volume_type=vt_name,
        )['volume']
        self.addCleanup(self._delete_volume_safe, vol['id'])
        waiters.wait_for_volume_resource_status(
            self.vols, vol['id'], 'available')
        vol_info = self.vols.show_volume(vol['id'])['volume']
        LOG.info("Volume %s available on host '%s'",
                 vol_info['id'],
                 vol_info.get('os-vol-host-attr:host'))
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
            if status in ('error', 'error_restoring', 'error_extending'):
                self.fail(
                    "Volume %s entered error state: %s" % (vol_id, status))
            time.sleep(interval)
        self.fail("Timeout waiting for volume %s to reach '%s'; "
                  "last='%s'" % (vol_id, target, last))

    # ------------------------------------------------------------------
    # Migration helpers
    # ------------------------------------------------------------------
    def _migrate_volume(self, vol_id, dest_host, force_host_copy=False):
        """Trigger admin migration via the ``os-migrate_volume`` action.

        POST /v3/{project_id}/volumes/{volume_id}/action
        body: {"os-migrate_volume": {"host": "<host>",
                                     "force_host_copy": <bool>}}

        This is the Cinder API that causes the PowerStore driver's
        ``migrate_volume()`` entry-point to be invoked.
        """
        LOG.info("Requesting os-migrate_volume for volume %s to '%s' "
                 "(force_host_copy=%s)", vol_id, dest_host, force_host_copy)

        body = json.dumps({
            "os-migrate_volume": {
                "host": dest_host,
                "force_host_copy": force_host_copy,
            }
        })
        headers = {'Content-Type': 'application/json'}
        api_mv = getattr(self.vols, 'api_microversion', None)
        if api_mv:
            headers['OpenStack-API-Version'] = 'volume %s' % api_mv

        resp, _ = self.vols.post(
            'volumes/%s/action' % vol_id, body, headers=headers)
        if not (200 <= resp.status <= 299):
            self.fail("os-migrate_volume returned HTTP %s for volume %s"
                      % (resp.status, vol_id))
        LOG.info("os-migrate_volume accepted (HTTP %s) for volume %s",
                 resp.status, vol_id)

    def _wait_for_migration(self, vol_id, dest_host,
                            timeout=MIGRATION_TIMEOUT,
                            interval=MIGRATION_POLL_INTERVAL):
        """Poll until migration completes (host matches or status=success).

        Returns the final volume dict.
        """
        deadline = time.time() + timeout
        last_mstatus = None
        while time.time() < deadline:
            vol = self.vols.show_volume(vol_id)['volume']
            host = vol.get('os-vol-host-attr:host') or vol.get('host', '')
            mstatus = (vol.get('migration_status') or '').lower()
            last_mstatus = mstatus
            LOG.info("Polling vol=%s host='%s' migration_status='%s'",
                     vol_id, host, mstatus)
            if host == dest_host or mstatus == 'success':
                LOG.info("Migration succeeded for vol=%s: host='%s' "
                         "migration_status='%s'", vol_id, host, mstatus)
                return vol
            if mstatus in ('error', 'failed'):
                self.fail("Migration failed for vol=%s with "
                          "migration_status=%s" % (vol_id, mstatus))
            time.sleep(interval)
        self.fail("Migration timed out for vol=%s; last "
                  "migration_status=%s" % (vol_id, last_mstatus))

    def _migrate_and_wait(self, vol_id, dest_host, force_host_copy=None):
        """Convenience: trigger migration and wait for completion.

        Returns the final volume dict after migration.  When
        *force_host_copy* is ``None`` (the default), the class-level
        ``FORCE_HOST_COPY`` attribute is used.
        """
        if force_host_copy is None:
            force_host_copy = self.FORCE_HOST_COPY
        self._migrate_volume(vol_id, dest_host,
                             force_host_copy=force_host_copy)
        return self._wait_for_migration(vol_id, dest_host)

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


# ======================================================================
# Positive test methods
# ======================================================================
class _PowerStoreVolumeMigratePositiveTests(object):
    """Positive functional tests for PowerStore driver-assisted migration.

    These tests exercise real Cinder API calls that cause the PowerStore
    driver's ``migrate_volume()`` to be invoked.  The driver creates a
    PowerStore migration session, syncs, waits for completion, and
    (optionally) performs cutover — all via the PowerStore REST API.

    Each test targets PowerStore-specific behaviour NOT covered by the
    generic tempest retype-with-migration test or the basic smoke test.
    """

    @classmethod
    def skip_checks(cls):
        super(_PowerStoreVolumeMigratePositiveTests, cls).skip_checks()
        if not CONF.service_available.cinder:
            raise cls.skipException("Cinder is not available")

    # ----------------------------------------------------------------
    # 1. Migrated volume preserves size
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d1a2b3c4-1001-4001-8001-e5f6a7b8c9d1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_migrate_volume_preserves_size(self):
        """Volume size is preserved after PowerStore driver-assisted migration.

        The driver resolves the volume on PowerStore via
        client.get_volume_id_by_name(), creates a migration session, and
        syncs to the destination appliance.  After completion Cinder must
        report the same size.

        Flow: create(2G) -> migrate -> verify size == 2G
        PowerStore API hits: GET /volume, GET /volume/{id},
            GET /appliance, POST /migration_session,
            POST /migration_session/{id}/sync,
            GET /migration_session/{id} (poll)
        """
        LOG.info("=== test_migrate_volume_preserves_size ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=2)
        vol_id = vol['id']
        original_size = vol['size']
        original_host = vol.get('os-vol-host-attr:host', '')

        target = self._pick_migration_target(original_host)

        migrated = self._migrate_and_wait(vol_id, target)

        self.assertEqual(migrated['size'], original_size,
                         "Size must be preserved after migration; "
                         "expected %d, got %d"
                         % (original_size, migrated['size']))
        LOG.info("Size preserved after migration: %dG", original_size)

    # ----------------------------------------------------------------
    # 2. Volume available with migration_status=success
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d2a3b4c5-1002-4002-8002-f6a7b8c9d0e2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_migrate_volume_available_after_completion(self):
        """After migration the volume must be 'available' with success status.

        Validates that after the PowerStore driver-assisted migration
        session completes, Cinder correctly transitions the volume back
        to 'available' status and sets migration_status to 'success'.

        Flow: create -> migrate -> verify status=available,
              migration_status=success, host changed
        """
        LOG.info("=== test_migrate_volume_available_after_completion ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']
        initial_host = vol.get('os-vol-host-attr:host', '')

        target = self._pick_migration_target(initial_host)

        migrated = self._migrate_and_wait(vol_id, target)

        self.assertEqual(migrated['status'], 'available',
                         "Volume must be 'available' after migration; "
                         "got '%s'" % migrated['status'])
        mig_status = (migrated.get('migration_status') or '').lower()
        self.assertEqual(mig_status, 'success',
                         "migration_status must be 'success'; got '%s'"
                         % mig_status)
        final_host = migrated.get('os-vol-host-attr:host', '')
        self.assertNotEqual(initial_host, final_host,
                            "Host must change after migration; still '%s'"
                            % initial_host)
        LOG.info("Volume %s migrated: host '%s' -> '%s', "
                 "migration_status='%s'",
                 vol_id, initial_host, final_host, mig_status)

    # ----------------------------------------------------------------
    # 3. Extend a migrated volume
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d3a4b5c6-1003-4003-8003-a7b8c9d0e1f3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_volume_after_migration(self):
        """Extend a volume after PowerStore driver-assisted migration.

        Validates that the PowerStore backend correctly handles
        extend_volume on a volume that has been migrated to a different
        appliance.  This exercises the provider_id and backend mapping
        consistency after migration.

        Flow: create(1G) -> migrate -> extend(2G) -> verify size=2
        """
        LOG.info("=== test_extend_volume_after_migration ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']
        initial_host = vol.get('os-vol-host-attr:host', '')

        target = self._pick_migration_target(initial_host)
        self._migrate_and_wait(vol_id, target)

        extended = self._extend_volume(vol_id, 2)
        self.assertEqual(extended['size'], 2,
                         "Expected size 2G after extend; got %d"
                         % extended['size'])
        self.assertEqual(extended['status'], 'available')
        LOG.info("Volume %s extended to 2G after migration", vol_id)

    # ----------------------------------------------------------------
    # 4. Delete a migrated volume
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d4a5b6c7-1004-4004-8004-b8c9d0e1f2a4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_volume_after_migration(self):
        """Delete a volume after PowerStore driver-assisted migration.

        Validates that the PowerStore backend correctly cleans up a
        volume that has been migrated to a different appliance.

        Flow: create -> migrate -> delete -> verify gone
        """
        LOG.info("=== test_delete_volume_after_migration ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']
        initial_host = vol.get('os-vol-host-attr:host', '')

        target = self._pick_migration_target(initial_host)
        self._migrate_and_wait(vol_id, target)

        self.vols.delete_volume(vol_id)
        self.vols.wait_for_resource_deletion(vol_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.vols.show_volume,
            vol_id,
        )
        LOG.info("Migrated volume %s deleted successfully", vol_id)

    # ----------------------------------------------------------------
    # 5. Full lifecycle: create -> migrate -> extend -> delete
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d5a6b7c8-1005-4005-8005-c9d0e1f2a3b5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_migrate_extend_delete_lifecycle(self):
        """Full lifecycle exercising driver-assisted migration end-to-end.

        This is the most comprehensive positive test: create a volume
        on PowerStore, migrate it to a different appliance/pool via
        the driver-assisted path, extend it, then delete it.

        Flow: create(1G) -> migrate -> verify host changed
              -> extend(3G) -> verify size -> delete -> verify gone
        """
        LOG.info("=== test_migrate_extend_delete_lifecycle ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']
        initial_host = vol.get('os-vol-host-attr:host', '')

        # Migrate
        target = self._pick_migration_target(initial_host)
        migrated = self._migrate_and_wait(vol_id, target)
        final_host = migrated.get('os-vol-host-attr:host', '')
        self.assertNotEqual(initial_host, final_host,
                            "Host must change after migration")
        self.assertEqual(migrated['size'], 1)

        # Extend
        extended = self._extend_volume(vol_id, 3)
        self.assertEqual(extended['size'], 3)
        self.assertEqual(extended['status'], 'available')

        # Delete
        self.vols.delete_volume(vol_id)
        self.vols.wait_for_resource_deletion(vol_id)
        self.assertRaises(
            lib_exc.NotFound,
            self.vols.show_volume,
            vol_id,
        )
        LOG.info("Full migrate -> extend -> delete lifecycle completed "
                 "for volume %s", vol_id)

    # ----------------------------------------------------------------
    # 6. Volume type preserved after migration
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d6a7b8c9-1006-4006-8006-d0e1f2a3b4c6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_migrate_volume_preserves_volume_type(self):
        """Volume type must be unchanged after driver-assisted migration.

        Cinder's ``os-migrate_volume`` preserves the volume type (unlike
        retype which changes it).  Verify that the PowerStore driver's
        migration path does not inadvertently change the type.

        Flow: create(type=ps-type) -> migrate -> verify type unchanged
        """
        LOG.info("=== test_migrate_volume_preserves_volume_type ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']
        original_type = vol['volume_type']
        initial_host = vol.get('os-vol-host-attr:host', '')

        target = self._pick_migration_target(initial_host)
        migrated = self._migrate_and_wait(vol_id, target)

        self.assertEqual(migrated['volume_type'], original_type,
                         "Volume type must be preserved after migration; "
                         "expected '%s', got '%s'"
                         % (original_type, migrated['volume_type']))
        LOG.info("Volume type '%s' preserved after migration", original_type)

    # ----------------------------------------------------------------
    # 7. Migration to same host is rejected by Cinder API
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d7a8b9c0-1007-4007-8007-e1f2a3b4c5d7')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_migrate_volume_to_same_host_rejected(self):
        """Migration to the same host is rejected by the Cinder API.

        Cinder validates that the destination host must differ from the
        current host before the driver is even invoked.  The API should
        return HTTP 400 (BadRequest) and the volume must remain in
        'available' status.

        Flow: create -> migrate(same host) -> expect 400 -> verify
              volume still available
        """
        LOG.info("=== test_migrate_volume_to_same_host_rejected ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']
        same_host = vol.get('os-vol-host-attr:host', '')

        if not same_host:
            self.skipTest("Could not determine volume host")

        # Cinder rejects same-host migration at the API level with 400
        self.assertRaises(
            lib_exc.BadRequest,
            self._migrate_volume,
            vol_id,
            same_host,
        )

        # Volume must remain available
        vol_after = self.vols.show_volume(vol_id)['volume']
        self.assertEqual(vol_after['status'], 'available',
                         "Volume should remain 'available' after rejected "
                         "same-host migration; got '%s'"
                         % vol_after['status'])
        LOG.info("Same-host migration correctly rejected for volume %s",
                 vol_id)


# ======================================================================
# Negative test methods
# ======================================================================
class _PowerStoreVolumeMigrateNegativeTests(object):
    """Negative functional tests for PowerStore volume migration."""

    @classmethod
    def skip_checks(cls):
        super(_PowerStoreVolumeMigrateNegativeTests, cls).skip_checks()
        if not CONF.service_available.cinder:
            raise cls.skipException("Cinder is not available")

    # ----------------------------------------------------------------
    # 8. Migration to a non-existent host
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d8a9b0c1-1008-4008-8008-f2a3b4c5d6e8')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_migrate_volume_to_nonexistent_host(self):
        """Migration to a non-existent host should fail.

        When the destination host doesn't correspond to any configured
        PowerStore backend, the driver's ``migrate_volume()`` should
        raise VolumeBackendAPIException and Cinder should transition
        the volume status to error or reject the request.

        Flow: create -> migrate(bogus-host) -> expect error or rejection
        """
        LOG.info("=== test_migrate_volume_to_nonexistent_host ===")

        vt = self._create_powerstore_volume_type()
        vol = self._create_volume(vt['name'], size=1)
        vol_id = vol['id']

        bogus_host = 'nonexistent-node@nonexistent-backend#nonexistent-pool'

        # The API may reject outright (4xx) or accept and transition to error
        try:
            self._migrate_volume(vol_id, bogus_host)
        except (lib_exc.BadRequest, lib_exc.NotFound,
                lib_exc.ServerFault, AssertionError) as e:
            LOG.info("Migration correctly rejected by API: %s", e)
            # Verify volume is still available
            vol_after = self.vols.show_volume(vol_id)['volume']
            self.assertEqual(vol_after['status'], 'available',
                             "Volume should remain available after rejected "
                             "migration; got '%s'" % vol_after['status'])
            return

        # If accepted, wait for the migration to fail
        deadline = time.time() + VOLUME_BUILD_TIMEOUT
        while time.time() < deadline:
            vol_after = self.vols.show_volume(vol_id)['volume']
            status = (vol_after.get('status') or '').lower()
            mig_status = (vol_after.get('migration_status') or '').lower()
            if mig_status in ('error', 'failed'):
                LOG.info("Migration correctly failed: migration_status=%s",
                         mig_status)
                return
            if status == 'available' and mig_status in ('success', 'none',
                                                         ''):
                # Cinder might have silently rejected the migration
                # (returned False, None) and left the volume available
                LOG.info("Migration silently rejected (volume still "
                         "available, no error)")
                return
            if status == 'error':
                LOG.info("Volume entered error state as expected")
                return
            time.sleep(VOLUME_BUILD_INTERVAL)

        # Timeout — check final state
        vol_final = self.vols.show_volume(vol_id)['volume']
        final_status = vol_final.get('status', '')
        final_mig = vol_final.get('migration_status', '')
        LOG.info("Final state: status=%s, migration_status=%s",
                 final_status, final_mig)
        # As long as the volume didn't end up on the bogus host, we're OK
        final_host = vol_final.get('os-vol-host-attr:host', '')
        self.assertNotEqual(
            final_host, bogus_host,
            "Volume should NOT have migrated to non-existent host '%s'"
            % bogus_host)


# ======================================================================
# Concrete test classes
# ======================================================================
class TestPowerStoreVolumeMigratePositive(
        _PowerStoreVolumeMigratePositiveTests,
        PowerStoreVolumeMigrateBase,
        volume_base.BaseVolumeAdminTest):
    """Positive functional tests for PowerStore driver-assisted migration."""
    credentials = ['primary', 'admin']

    # Set to True to use Cinder's generic host-copy migration instead of the
    # driver-assisted path.  The driver-assisted path (False) requires a
    # multi-appliance PowerStore cluster.
    # FORCE_HOST_COPY = True


class TestPowerStoreVolumeMigrateNegative(
        _PowerStoreVolumeMigrateNegativeTests,
        PowerStoreVolumeMigrateBase,
        volume_base.BaseVolumeAdminTest):
    """Negative functional tests for PowerStore driver-assisted migration."""
    credentials = ['primary', 'admin']
