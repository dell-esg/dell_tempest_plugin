
# -*- coding: utf-8 -*-
"""
Tempest functional tests for Dell PowerStore driver-assisted migration.

References:
- OpenStack Cinder Admin Guide (Volume Migration: host@backend#pool, volumes action)
  https://docs.openstack.org/cinder/latest/admin/volume-migration.html
- OpenStack Cinder Contributor Docs (Migration scenarios; retype on-demand)
  https://docs.openstack.org/cinder/latest/contributor/migration.html
- Platform9 KB (CLI migration syntax & host string format)
  https://platform9.com/kb/openstack/how-to-migrate-volumes-using-cli
"""

import time
import json

import dell_tempest_plugin.tests.base.test_dell_base as dell_base

from tempest.lib import decorators
from tempest.common import waiters  
from tempest.lib import exceptions as lib_exc
from tempest.lib.common.utils import data_utils
from tempest import config
from tempest.api.volume import base as volume_base

from oslo_log import log as logging

CONF = config.CONF
LOG = logging.getLogger(__name__)

MIGRATION_TIMEOUT = 1800  # seconds (adjust for your env)
MIGRATION_POLL_INTERVAL = 10  # seconds


class PowerStoreTempestTest(dell_base.BaseTempestTest):
    backend_name = "powerstore"
    backend_id = "powerstore-backend-id"

    @decorators.idempotent_id('328faacf-1dcc-40bc-a92c-92a9b5a1c4fe')
    def test_failover_host(self):
        LOG.info("Executing: PowerStoreTempestTest.test_failover_host")
        if not getattr(dell_base.CONF.volume_feature_enabled, 'replication', False):
            self.skipTest("Skipping test: replication not enabled")
        self._run_failover_test()


class PowerStoreMigrateVolumeTest(volume_base.BaseVolumeAdminTest):
    """Functional tests for Cinder driver-assisted migration (PowerStore)."""
    credentials = ['primary', 'admin']

    # ------------------------------
    # Admin client resolution
    # ------------------------------
    def _get_admin_volumes_client(self):
        """Returns the admin volumes client or skips if unavailable."""
        client = getattr(self, 'admin_volumes_client', None)
        if client:
            return client

        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (getattr(os_admin, 'volumes_client_latest', None) or
                      getattr(os_admin, 'volumes_v3_client', None) or
                      getattr(os_admin, 'volumes_client', None))
            if client:
                return client

        self.skip("Admin volumes client not found. Ensure 'admin' is in "
                  "the credentials list and Cinder is enabled.")

    def _get_admin_volume_types_client(self):
        """Returns the admin volume types client or skips if unavailable."""
        client = getattr(self, 'admin_volume_types_client', None)
        if client:
            return client

        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = (getattr(os_admin, 'volume_types_v3_client', None) or
                      getattr(os_admin, 'volume_types_client', None))
            if client:
                return client

        self.skip("Admin volume types client not found. Ensure 'admin' is in "
                  "the credentials list and Cinder is enabled.")

    def _get_admin_scheduler_stats_client(self):
        client = getattr(self, 'admin_scheduler_stats_client', None)
        if client:
            return client

        os_admin = getattr(self, 'os_admin', None)
        if os_admin:
            client = getattr(os_admin, 'scheduler_stats_client', None)
            if client:
                return client

        LOG.info("Admin scheduler stats client not found; skipping pool discovery.")
        return None

    # ------------------------------
    # Setup
    # ------------------------------
    @classmethod
    def resource_setup(cls):
        # Only perform feature gate checks here; clients and discovery happen in setUp()
        super(PowerStoreMigrateVolumeTest, cls).resource_setup()
        if not getattr(CONF.volume_feature_enabled, 'volume_types', False):
            raise cls.skipException("Volume types are not enabled in Tempest config")

    def setUp(self):
        """Instance-level setup so clients exist and are safe to use."""
        super(PowerStoreMigrateVolumeTest, self).setUp()

        self.vols = self._get_admin_volumes_client()
        self.vtypes = self._get_admin_volume_types_client()
        self.sched = self._get_admin_scheduler_stats_client()

        if self.vols is None or self.vtypes is None:
            self.skip("Cannot resolve volume/volume_types clients (admin or primary). "
                      "Check tempest.conf endpoints.")

        # Optional discovery of PowerStore targets (host@backend#pool)
        self.powerstore_hosts = self._discover_powerstore_hosts()

    def _discover_powerstore_hosts(self):
        """Best-effort discovery via scheduler stats pools (optional)."""
        if not self.sched:
            return []
        candidates = set()
        try:
            pools_resp = self.sched.list_pools(detail=True)
            for p in pools_resp.get('pools', []):
                name = p.get('name')  # host@backend#pool
                caps = p.get('capabilities', {}) or {}
                backend_name = caps.get('volume_backend_name') or ''
                if (name and 'powerstore' in name.lower()) or ('powerstore' in backend_name.lower()):
                    candidates.add(name)
            if candidates:
                LOG.info("Found PowerStore pools: %s", sorted(candidates))
        except Exception as e:
            LOG.warning("Pool discovery failed: %s", e)
        return sorted(candidates)

    # ------------------------------
    # Helpers
    # ------------------------------
    def _parse_host_backend_pool(self, host_str):
        """
        Parse 'host@backend#pool' into (host, backend, pool).
        Handles cases like 'host@backend' (no pool) too.
        """
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

    def _create_volume_type_for_backend(self, backend_name: str):
        vt = self.vtypes.create_volume_type(
            name=data_utils.rand_name(f'ps-type-{backend_name}'),
        )['volume_type']
        # Safe deletion on cleanup
        self.addCleanup(self._delete_volume_type_safe, vt['id'])
        LOG.info("Created volume type %s (%s) with volume_backend_name=%s",
                 vt['name'], vt['id'], backend_name)
        return vt

    def _create_powerstore_volume_type(self):
        """
        Create a volume type pointing to the PowerStore backend.
        Adjust backend_name if your cinder.conf uses a different string.
        """
        backend_name = 'powerstore'  # e.g., 'Dell PowerStore' if configured differently
        vt = self.vtypes.create_volume_type(
            name=data_utils.rand_name('powerstore-type'),
        )['volume_type']
        self.addCleanup(self.vtypes.delete_volume_type, vt['id'])
        LOG.info("Created volume type %s (%s) with backend_name=%s", vt['name'], vt['id'], backend_name)
        return vt

    def _create_volume(self, vt_name, size=1):
        """Create a volume and wait until it is 'available'."""
        vol = self.vols.create_volume(
            name=data_utils.rand_name('ps-migrate-vol'),
            size=size,
            volume_type=vt_name
        )['volume']
        # Robust cleanup (delete and wait until gone)
        self.addCleanup(self._delete_volume_and_wait, vol['id'])

        # Wait until it becomes 'available'
        try:
            waiters.wait_for_volume_resource_status(self.vols, vol['id'], 'available')
        except Exception as e:
            LOG.warning("Shared waiter unavailable/failed: %s; using local poller.", e)
            self._wait_until_volume_status(vol['id'], target='available',
                                           timeout=CONF.volume.build_timeout,
                                           interval=CONF.volume.build_interval)
        vol_info = self.vols.show_volume(vol['id'])['volume']
        host = vol_info.get('os-vol-host-attr:host') or vol_info.get('host')
        LOG.info("Volume %s is available on host '%s'", vol_info['id'], host)
        return vol_info

    def _wait_until_volume_status(self, volume_id, target='available', timeout=600, interval=5):
        """Fallback waiter for older Tempest builds."""
        end = time.time() + timeout
        last = None
        while time.time() < end:
            v = self.vols.show_volume(volume_id)['volume']
            status = (v.get('status') or '').lower()
            last = status
            if status == target:
                return
            if status in ('error', 'error_restoring', 'error_extending'):
                self.fail(f"Volume {volume_id} entered failure status: {status}")
            time.sleep(interval)
        self.fail(f"Timeout waiting for volume {volume_id} to reach '{target}', last='{last}'")

    # --- deletion waiter improvements ---
    def _has_waiter(self, name: str) -> bool:
        """Return True if tempest.common.waiters exposes the given attribute."""
        try:
            import tempest.common.waiters as w
            return hasattr(w, name)
        except Exception:
            return False

    def _wait_until_volume_deleted(self, vol_id, timeout=600, interval=1.0, backoff=1.5, max_interval=10.0):
        """Local fallback waiter for deletion: polls show_volume until NotFound."""
        deadline = time.time() + timeout
        delay = max(0.1, float(interval))
        last_err = None

        while time.time() < deadline:
            try:
                self.vols.show_volume(vol_id)  # still present
            except lib_exc.NotFound:
                return
            except Exception as e:
                last_err = e
                LOG.debug("show_volume(%s) error during deletion wait: %s", vol_id, e)

            time.sleep(delay)
            delay = min(delay * backoff, max_interval)

        msg = f"Timeout waiting for volume {vol_id} deletion (last error: {last_err})"
        LOG.error(msg)
        raise lib_exc.TempestException(msg)

    def _delete_volume_and_wait(self, vol_id, timeout=600, interval=1.0):
        """Delete a volume and wait until it is gone (feature-probe Tempest waiters quietly)."""
        LOG.info("Deleting volume %s and waiting until it vanishes...", vol_id)
        try:
            self.vols.delete_volume(vol_id)
        except lib_exc.NotFound:
            LOG.info("Volume %s not found at delete request time; treating as deleted.", vol_id)
            return
        except Exception as e:
            LOG.debug("delete_volume(%s) raised: %s (continuing to wait for deletion)", vol_id, e)

        try:
            import tempest.common.waiters as w

            if self._has_waiter('wait_for_resource_deletion'):
                LOG.debug("Using Tempest waiters.wait_for_resource_deletion for volume %s", vol_id)
                w.wait_for_resource_deletion(self.vols, vol_id, timeout=timeout, interval=interval)
                return

            if self._has_waiter('wait_for_volume_resource_deletion'):
                LOG.debug("Using Tempest waiters.wait_for_volume_resource_deletion for volume %s", vol_id)
                w.wait_for_volume_resource_deletion(self.vols, vol_id, timeout=timeout, interval=interval)
                return

        except Exception as e:
            LOG.debug("Tempest waiter import/use failed for volume %s: %s; using local poller.", vol_id, e)

        self._wait_until_volume_deleted(vol_id, timeout=timeout, interval=interval)
        LOG.info("Volume %s deletion confirmed.", vol_id)

    
    def _migrate_volume_admin(self, volume_id, dest_host, force_host_copy=False):
        """Admin migration using the legacy 'os-migrate_volume' action.

        This is the canonical path for Scenario 1 (same type/backend):
        POST /v3/{project_id}/volumes/{volume_id}/action
        body: {"os-migrate_volume": {"host": "<host@backend#pool>", "force_host_copy": <bool>}}

        Notes:
        - The destination must satisfy the current volume type's extra specs.
        - 'dest_host' must be in the form 'host@backend#pool'.

        Raises:
        lib_exc.TempestException on any non-2xx response or request failure.
        """
        LOG.info(
            "Requesting legacy os-migrate_volume for volume %s to '%s' (force_host_copy=%s)",
            volume_id, dest_host, force_host_copy
        )

        headers = {'Content-Type': 'application/json'}

        # If the Tempest client exposes a microversion, include it (harmless for legacy action).
        api_mv = getattr(self.vols, 'api_microversion', None)
        if api_mv:
            headers['OpenStack-API-Version'] = f"volume {api_mv}"

        legacy_body = {
            "os-migrate_volume": {
                "host": dest_host,
                "force_host_copy": force_host_copy
            }
        }

        try:
            resp, _ = self.vols.post(
                f"volumes/{volume_id}/action",
                json.dumps(legacy_body),
                headers=headers
            )
            if 200 <= resp.status <= 299:
                LOG.info(
                    "Legacy 'os-migrate_volume' accepted (HTTP %s) for volume %s",
                    resp.status, volume_id
                )
                return resp.status
            # Non-2xx: surface as TempestException
            raise lib_exc.TempestException(
                f"Legacy os-migrate_volume returned HTTP {resp.status} for volume {volume_id}"
            )
        except Exception as e:
            msg = (
                f"Legacy os-migrate_volume request failed for volume {volume_id} "
                f"to '{dest_host}': {e}"
            )
            LOG.error(msg)
            raise lib_exc.TempestException(msg)

    def _wait_for_migration(self, volume_id, target_host):
        """Poll until migration completes (host equals target or status=='success')."""
        deadline = time.time() + MIGRATION_TIMEOUT
        last_status = None
        while time.time() < deadline:
            vol = self.vols.show_volume(volume_id)['volume']
            host = vol.get('os-vol-host-attr:host') or vol.get('host')
            mstatus = (vol.get('migration_status') or '').lower()
            last_status = mstatus
            LOG.info("Polling vol=%s host='%s' migration_status='%s'",
                     volume_id, host, mstatus)
            if host == target_host or mstatus == 'success':
                LOG.info("Migration succeeded for vol=%s: host='%s', status='%s'",
                         volume_id, host, mstatus)
                return vol
            if mstatus in ('error', 'failed'):
                self.fail(f"Migration failed for vol={volume_id} with status={mstatus}")
            time.sleep(MIGRATION_POLL_INTERVAL)
        self.fail(f"Migration timed out for vol={volume_id}; last status={last_status}")

    def _delete_volume_type_safe(self, type_id, timeout=300, interval=5):
        """Try to delete a type; if still in use, retry briefly then log."""
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
            LOG.warning("Final attempt to delete volume type %s failed: %s", type_id, e)

    def _assert_migration_preconditions_and_pick_target(self, vol, candidate_targets):
        """Return a target host different from current; raise skip/fail if none suitable."""
        current = vol.get('os-vol-host-attr:host') or vol.get('host') or ''
        status = (vol.get('status') or '').lower()
        mig_status = (vol.get('migration_status') or '').lower()

        if status not in ('available', 'in-use'):
            self.fail(f"Volume {vol['id']} status '{status}' not migratable; expected available/in-use.")
        if mig_status in ('starting', 'migrating', 'completing'):
            self.fail(f"Volume {vol['id']} migration_status '{mig_status}' not terminal; wait until 'none' or 'success'.")

        target = None
        for t in candidate_targets:
            if t and t != current:
                target = t
                break
        if not target:
            self.skip(f"No suitable target different from current '{current}'. Candidates: {candidate_targets}")
        return target

    # ------------------------------
    # Functional tests
    # ------------------------------
    
    @decorators.idempotent_id('7d9d2e7a-22e6-4f58-b96e-6f1ae9b8f9aa')
    def test_migrate_volume_between_powerstore_hosts(self):
        # Need at least two distinct PowerStore targets.
        if len(self.powerstore_hosts) < 2:
            self.skip("Only one or zero PowerStore pools/hosts discovered; skipping cross-host migration.")

        source_str, target_str = self.powerstore_hosts[0], self.powerstore_hosts[1]

        # Parse both endpoints: host@backend#pool -> (host, backend, pool)
        src_host, src_backend, src_pool = self._parse_host_backend_pool(source_str)
        tgt_host, tgt_backend, tgt_pool = self._parse_host_backend_pool(target_str)
        LOG.info(
            "Cross-host migration "
            "source='%s' (host=%s backend=%s pool=%s) "
            "target='%s' (host=%s backend=%s pool=%s)",
            source_str, src_host, src_backend, src_pool,
            target_str, tgt_host, tgt_backend, tgt_pool
        )

        # IMPORTANT: pin the create to the source backend (e.g., 'powerstore1').
        vt = self._create_volume_type_for_backend(src_backend)

        # Create the volume; wait for 'available'.
        vol = self._create_volume(vt_name=vt['name'], size=1)
        vol_id = vol['id']

        # Determine the actual initial placement (host@backend#pool if exposed).
        initial_host_str = vol.get('os-vol-host-attr:host') or vol.get('host') or ''
        cur_host, cur_backend, cur_pool = self._parse_host_backend_pool(initial_host_str)
        LOG.info("Initial volume host string: '%s' -> parsed host=%s backend=%s pool=%s",
                initial_host_str, cur_host, cur_backend, cur_pool)

        # If the volume already landed on the intended target, reverse source/target.
        already_on_target = (
            (initial_host_str == target_str) or
            (cur_backend and tgt_backend and cur_backend == tgt_backend and
            ((cur_pool and tgt_pool and cur_pool == tgt_pool) or (tgt_pool is None)))
        )
        if already_on_target:
            LOG.warning("Volume already on intended target (%s). Reversing source/target.", target_str)
            # Swap source and target strings
            source_str, target_str = target_str, source_str
            # Re-parse after swap
            src_host, src_backend, src_pool = self._parse_host_backend_pool(source_str)
            tgt_host, tgt_backend, tgt_pool = self._parse_host_backend_pool(target_str)
            LOG.info(
                "Reversed endpoints: "
                "source='%s' (host=%s backend=%s pool=%s) "
                "target='%s' (host=%s backend=%s pool=%s)",
                source_str, src_host, src_backend, src_pool,
                target_str, tgt_host, tgt_backend, tgt_pool
            )

        # Final target sanity: pick a target different from current.
        target_str = self._assert_migration_preconditions_and_pick_target(vol, self.powerstore_hosts)

        # Decide migration path:
        # - Same backend: admin migrate (Scenario 1).
        # - Different backend: (hook for retype on-demand if you use it elsewhere).
        if src_backend == tgt_backend:
            LOG.info("Backends match (%s); using admin migrate to target '%s'.",
                    src_backend, target_str)
            self._migrate_volume_admin(volume_id=vol_id, dest_host=target_str, force_host_copy=False)

            # Wait for migration to complete.
            vol_after = self._wait_for_migration(volume_id=vol_id, target_host=target_str)
            final_host_str = vol_after.get('os-vol-host-attr:host') or vol_after.get('host') or ''
            LOG.info("Final volume host string after migration: '%s'", final_host_str)

            # Assert that placement changed.
            self.assertNotEqual(
                initial_host_str, final_host_str,
                f"Host did not change: initial='{initial_host_str}' final='{final_host_str}'"
            )
        else:
            LOG.info("Backends differ: %s -> %s; volume migration across backends requires retype "
                    "(on-demand) or a type without backend pinning.",
                    src_backend, tgt_backend)

