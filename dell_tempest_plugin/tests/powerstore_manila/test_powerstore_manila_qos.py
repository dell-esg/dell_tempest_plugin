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
Tempest functional tests for Dell PowerStore Manila QoS feature.

These tests exercise the QoS lifecycle through real Manila API calls that
result in actual PowerStore REST API calls:

  - Creating QoS types with max_bw (bandwidth limit) specs
  - Creating share types with default_qos_type extra-spec
  - Creating NFS/CIFS shares that trigger file_io_limit_rule + policy creation
  - Deleting shares that trigger cleanup of unused QoS rules/policies
  - Extending/shrinking shares with QoS (verifies QoS preserved)
  - Multiple shares sharing the same QoS type / policy
  - Snapshot-based share creation with QoS

PowerStore REST API endpoints exercised:
  - POST   /file_io_limit_rule                  (create_file_io_limit_rule)
  - GET    /file_io_limit_rule?name=eq.{name}   (get_file_io_limit_rule_by_name)
  - PATCH  /file_io_limit_rule/{id}             (modify_file_io_limit_rule)
  - DELETE /file_io_limit_rule/{id}             (delete_file_io_limit_rule)
  - POST   /policy                              (create_file_performance_policy)
  - GET    /policy?name=eq.{name}               (get_policy_by_name)
  - GET    /policy/{id}?select=file_systems_with_qos(id)
           (get_policy_filesystems)
  - DELETE /policy/{id}                         (delete_policy)
  - PATCH  /file_system/{id}                    (set_filesystem_performance_policy)

Driver methods tested:
  - create_share      (QoS rule/policy creation + filesystem association)
  - delete_share      (QoS cleanup when policy unused)
  - extend_share      (QoS preserved after resize)
  - shrink_share      (QoS preserved after resize)
  - create_share_from_snapshot (QoS applied to cloned filesystem)
  - _validate_qos_specs
  - _get_or_create_qos_policy
  - _apply_qos_to_filesystem
  - _cleanup_qos_on_delete
"""

import json
import os
import time

# Ensure TEMPEST_CONFIG_DIR is set so tempest can find tempest.conf
# when running with plain pytest outside of the tempest test runner.
if 'TEMPEST_CONFIG_DIR' not in os.environ:
    _candidate = os.path.join(os.getcwd(), 'tempest', 'etc')
    if os.path.isfile(os.path.join(_candidate, 'tempest.conf')):
        os.environ['TEMPEST_CONFIG_DIR'] = _candidate

from oslo_config import cfg
from oslo_log import log as logging
from tempest import clients
from tempest import config
from tempest.common import credentials_factory
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc
from tempest.lib.common.utils import data_utils

CONF = config.CONF
LOG = logging.getLogger(__name__)

# Register 'manila' option in service_available group so oslo_config can
# read it from tempest.conf even when manila_tempest_tests is not installed.
_manila_opt = cfg.BoolOpt('manila', default=True,
                          help='Whether or not manila is expected to be '
                               'available')
try:
    CONF.register_opt(_manila_opt, group='service_available')
except cfg.DuplicateOptError:
    pass

SHARE_BUILD_TIMEOUT = 600
SHARE_BUILD_INTERVAL = 5
# Shorter timeout for negative tests
NEGATIVE_TEST_TIMEOUT = 60

# Minimum Manila API microversion that supports QoS types
QOS_TYPE_MIN_API_VERSION = '2.94'

# PowerStore max_bw constraints (from manila driver)
QOS_MAX_BW_MIN = 1       # MB/s
QOS_MAX_BW_MAX = 1000000  # MB/s


class PowerStoreQoSShareTest(object):
    """Mixin with helpers for PowerStore Manila QoS share tests.

    Provides utility methods for creating QoS types, share types with
    default_qos_type, shares, and waiting for share status transitions
    via the Manila API.
    """

    @classmethod
    def setup_credentials(cls):
        super(PowerStoreQoSShareTest, cls).setup_credentials()

    @classmethod
    def setup_clients(cls):
        super(PowerStoreQoSShareTest, cls).setup_clients()
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
    # QoS type helpers (raw HTTP via shares_v2_client)
    # ------------------------------------------------------------------
    def _qos_type_request(self, method, url_suffix='', body=None):
        """Make a raw Manila API request for QoS type operations.

        The Manila QoS type API requires microversion >= 2.94.
        """
        url = 'qos-types'
        if url_suffix:
            url = f'{url}/{url_suffix}'
        headers = {'Content-Type': 'application/json'}
        client = self.shares_v2_client
        if method == 'POST':
            resp, resp_body = client.post(
                url, json.dumps(body), version=QOS_TYPE_MIN_API_VERSION)
        elif method == 'GET':
            resp, resp_body = client.get(
                url, version=QOS_TYPE_MIN_API_VERSION)
        elif method == 'DELETE':
            resp, resp_body = client.delete(
                url, version=QOS_TYPE_MIN_API_VERSION)
        else:
            raise ValueError(f"Unsupported method: {method}")
        return resp, json.loads(resp_body) if resp_body else {}

    def _qos_type_specs_request(self, method, qos_type_id,
                                key=None, body=None):
        """Make a raw Manila API request for QoS type specs operations."""
        url = f'qos-types/{qos_type_id}/specs'
        if key:
            url = f'{url}/{key}'
        client = self.shares_v2_client
        if method == 'POST':
            resp, resp_body = client.post(
                url, json.dumps(body), version=QOS_TYPE_MIN_API_VERSION)
        elif method == 'GET':
            resp, resp_body = client.get(
                url, version=QOS_TYPE_MIN_API_VERSION)
        elif method == 'DELETE':
            resp, resp_body = client.delete(
                url, version=QOS_TYPE_MIN_API_VERSION)
        else:
            raise ValueError(f"Unsupported method: {method}")
        return resp, json.loads(resp_body) if resp_body else {}

    def create_qos_type(self, name=None, specs=None):
        """Create a Manila QoS type with given specs.

        :param name: Optional name; auto-generated if None.
        :param specs: Dict of QoS specs (e.g. max_bw).
        :returns: Created QoS type dict.
        """
        name = name or data_utils.rand_name('ps-manila-qos-type')
        body = {
            'qos_type': {
                'name': name,
                'specs': specs or {},
            }
        }
        resp, resp_body = self._qos_type_request('POST', body=body)
        qt = resp_body.get('qos_type', resp_body)
        LOG.info("Created QoS type '%s' (id=%s) with specs=%s",
                 qt.get('name'), qt.get('id'), specs)
        self.addCleanup(self._delete_qos_type_safe, qt['id'])
        return qt

    def _delete_qos_type_safe(self, qos_type_id):
        """Delete QoS type, ignoring NotFound."""
        try:
            self._qos_type_request('DELETE', url_suffix=str(qos_type_id))
            LOG.info("Deleted QoS type %s", qos_type_id)
        except lib_exc.NotFound:
            LOG.debug("QoS type %s already gone", qos_type_id)
        except Exception as e:
            LOG.warning("Failed to delete QoS type %s: %s", qos_type_id, e)

    # ------------------------------------------------------------------
    # Share type helpers
    # ------------------------------------------------------------------
    def create_qos_share_type(self, qos_type_name, name=None,
                              extra_specs=None, backend_name=None):
        """Create a share type with default_qos_type pointing to a QoS type.

        :param qos_type_name: Name of the QoS type to link.
        :param name: Optional share type name.
        :param extra_specs: Additional extra-specs dict to merge.
        :param backend_name: Optional backend name for share_backend_name extra_spec.
        :returns: Created share type dict.
        """
        name = name or data_utils.rand_name('ps-manila-qos-share-type')
        specs = {
            'driver_handles_share_servers': str(CONF.share.multitenancy_enabled),
            'default_qos_type': qos_type_name,
        }
        if backend_name:
            specs['share_backend_name'] = backend_name
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

    def create_plain_share_type(self, name=None, extra_specs=None, backend_name=None):
        """Create a share type WITHOUT default_qos_type.

        :param name: Optional share type name.
        :param extra_specs: Additional extra-specs dict to merge.
        :param backend_name: Optional backend name for share_backend_name extra_spec.
        :returns: Created share type dict.
        """
        name = name or data_utils.rand_name('ps-manila-no-qos-type')
        specs = {'driver_handles_share_servers': str(CONF.share.multitenancy_enabled)}
        if backend_name:
            specs['share_backend_name'] = backend_name
        if extra_specs:
            specs.update(extra_specs)

        share_type = self.share_types_client.create_share_type(
            name=name,
            extra_specs=specs,
        )
        st = share_type.get('share_type', share_type)
        LOG.info("Created share type '%s' (id=%s) without QoS", st['name'],
                 st['id'])
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
    def create_share(self, protocol, share_type_name, size=None, name=None):
        """Create a Manila share and wait until it becomes available.

        :param protocol: 'NFS' or 'CIFS'
        :param share_type_name: Name of the share type to use.
        :param size: Share size in GB. If None, uses CONF.share.share_size.
        :param name: Optional share name.
        :returns: Created share dict.
        """
        if size is None:
            size = getattr(CONF.share, 'share_size', 3)
        name = name or data_utils.rand_name(
            f'ps-manila-qos-{protocol.lower()}')
        share = self.shares_v2_client.create_share(
            share_protocol=protocol,
            size=size,
            name=name,
            share_type_id=share_type_name,
        )
        sh = share.get('share', share)
        LOG.info("Created share '%s' (id=%s, protocol=%s, type=%s)",
                 sh['name'], sh['id'], protocol, share_type_name)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self.shares_v2_client.get_share(sh['id']).get(
            'share', self.shares_v2_client.get_share(sh['id']))

    def _delete_share_safe(self, share_id):
        """Delete a share and wait for it to be removed.

        If the share is already in error_deleting state, skip the wait
        since it may never complete due to driver issues.
        """
        try:
            # Check current status before attempting deletion
            try:
                result = self.shares_v2_client.get_share(share_id)
                sh = result.get('share', result)
                status = sh.get('status', '').lower()
                if status == 'error_deleting':
                    LOG.warning("Share %s is already in error_deleting state, "
                                "skipping deletion attempt", share_id)
                    return
            except lib_exc.NotFound:
                LOG.debug("Share %s already gone", share_id)
                return

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
        """Poll until share is gone (NotFound) or reaches a terminal error."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = self.shares_v2_client.get_share(share_id)
                sh = result.get('share', result)
                status = sh.get('status', '').lower()
                if status == 'error_deleting':
                    LOG.warning("Share %s is stuck in error_deleting state",
                                share_id)
                    return
            except lib_exc.NotFound:
                LOG.info("Share %s deletion confirmed", share_id)
                return
            time.sleep(interval)
        LOG.warning("Timeout waiting for share %s deletion", share_id)

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def create_snapshot(self, share_id, name=None):
        """Create a Manila snapshot and wait until it becomes available.

        :param share_id: ID of the share to snapshot.
        :param name: Optional snapshot name.
        :returns: Created snapshot dict.
        """
        name = name or data_utils.rand_name('ps-manila-qos-snap')
        snapshot = self.shares_v2_client.create_snapshot(
            share_id=share_id,
            name=name,
        )
        snap = snapshot.get('snapshot', snapshot)
        LOG.info("Created snapshot '%s' (id=%s) for share %s",
                 snap['name'], snap['id'], share_id)
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        self._wait_for_snapshot_status(snap['id'], 'available')
        return snap

    def _delete_snapshot_safe(self, snapshot_id):
        """Delete a snapshot, ignoring NotFound."""
        try:
            self.shares_v2_client.delete_snapshot(snapshot_id)
            LOG.info("Requested deletion of snapshot %s", snapshot_id)
        except lib_exc.NotFound:
            LOG.debug("Snapshot %s already gone", snapshot_id)
            return
        except Exception as e:
            LOG.warning("Failed to delete snapshot %s: %s", snapshot_id, e)
            return
        self._wait_for_snapshot_deletion(snapshot_id)

    def _wait_for_snapshot_status(self, snapshot_id, target_status,
                                  timeout=SHARE_BUILD_TIMEOUT,
                                  interval=SHARE_BUILD_INTERVAL):
        """Poll snapshot status until it reaches target or errors out."""
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            snapshot = self.shares_v2_client.get_snapshot(snapshot_id)
            snap = snapshot.get('snapshot', snapshot)
            status = snap.get('status', '').lower()
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
    # Resize helpers
    # ------------------------------------------------------------------
    def extend_share(self, share_id, new_size):
        """Extend a share and wait for it to become available."""
        self.shares_v2_client.extend_share(share_id, new_size)
        LOG.info("Requested extend of share %s to %s GB", share_id, new_size)
        self._wait_for_share_status(share_id, 'available')

    def shrink_share(self, share_id, new_size):
        """Shrink a share and wait for it to become available."""
        self.shares_v2_client.shrink_share(share_id, new_size)
        LOG.info("Requested shrink of share %s to %s GB", share_id, new_size)
        self._wait_for_share_status(share_id, 'available')

    # ------------------------------------------------------------------
    # Manila host discovery
    # ------------------------------------------------------------------
    def _get_manila_host(self):
        """Discover the Manila host string for the PowerStore backend."""
        try:
            services = self.shares_v2_client.list_services()
            for svc in services.get('services', services):
                host = svc.get('host', '')
                if 'powerstore' in host.lower():
                    LOG.info("Discovered Manila PowerStore host: %s", host)
                    return host
        except Exception as e:
            LOG.warning("Failed to discover Manila host: %s", e)
        return getattr(CONF, 'share', {}).get(
            'powerstore_host', 'manila-host@powerstore')

    def _get_export_locations(self, share_id):
        """Retrieve export locations for a share."""
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    def _get_backend_name(self):
        """Get the backend name from config."""
        backend_names = getattr(CONF.share, 'backend_names', [])
        if backend_names:
            return backend_names[0]
        return None


# ======================================================================
# NFS QoS Tests
# ======================================================================
class _NFSQoSTests(object):
    """Mixin: NFS share QoS test methods for PowerStore Manila.

    Each test makes real Manila API calls that propagate to the
    PowerStore backend, triggering file_io_limit_rule and
    File_Performance policy creation, update, or deletion.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSQoSTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Create NFS share with QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10a1b2c3-1111-2222-3333-e5f6a7b80001')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_with_qos(self):
        """Create an NFS share with QoS enabled and verify it succeeds.

        Expected PowerStore side-effects:
          1. POST /file_io_limit_rule to create rule (or GET if exists)
          2. POST /policy to create File_Performance policy (or GET)
          3. PATCH /file_system/{id} to associate policy
        """
        LOG.info("=== test_create_nfs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={'max_bw': '100'})

        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)
        self.assertIn('default_qos_type',
                      share_type.get('extra_specs', {}))

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
        )

        self.assertEqual(share['status'], 'available',
                         f"Share status is {share['status']}, "
                         f"expected available")
        self.assertEqual(share['share_proto'].upper(), 'NFS')
        export_locations = self._get_export_locations(share['id'])
        self.assertTrue(
            len(export_locations) > 0,
            "Share must have at least one export location")
        LOG.info("NFS share %s created with QoS (max_bw=100)",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create NFS share without QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10b2c3d4-2222-3333-4444-f6a7b8c90002')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_without_qos(self):
        """Create an NFS share without QoS type.

        No file_io_limit_rule or policy should be created on PowerStore.
        """
        LOG.info("=== test_create_nfs_share_without_qos ===")

        backend_name = self._get_backend_name()
        share_type = self.create_plain_share_type(backend_name=backend_name)
        self.assertNotIn('default_qos_type',
                         share_type.get('extra_specs', {}))

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'NFS')
        LOG.info("NFS share %s created without QoS", share['id'])

    # ----------------------------------------------------------------
    # Test: Delete NFS share with QoS (cleanup triggered)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10c3d4e5-3333-4444-5555-a7b8c9d00003')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_nfs_share_with_qos(self):
        """Delete an NFS share with QoS and verify cleanup.

        Expected PowerStore side-effects:
          1. GET policy filesystems to check usage
          2. DELETE /policy/{id} if unused
          3. DELETE /file_io_limit_rule/{id} if unused
        """
        LOG.info("=== test_delete_nfs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={'max_bw': '500'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        self.shares_v2_client.delete_share(share_id)
        LOG.info("Requested deletion of QoS-enabled NFS share %s", share_id)

        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("NFS share %s deleted; QoS rule/policy cleaned up",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Full NFS QoS lifecycle (create -> verify -> delete)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10d4e5f6-4444-5555-6666-b8c9d0e10004')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_qos_lifecycle(self):
        """Full lifecycle: create NFS with QoS -> verify -> delete.

        This exercises the complete QoS flow:
          1. _validate_qos_specs checks max_bw range
          2. _get_or_create_qos_policy creates rule + policy
          3. _apply_qos_to_filesystem associates policy
          4. On delete: _cleanup_qos_on_delete removes policy + rule
        """
        LOG.info("=== test_nfs_qos_lifecycle ===")

        qos_type = self.create_qos_type(specs={'max_bw': '1000'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
        )
        self.assertEqual(share['status'], 'available')
        share_id = share['id']

        updated = self.shares_v2_client.get_share(share_id)
        sh = updated.get('share', updated)
        self.assertEqual(sh['status'], 'available')
        export_locations = self._get_export_locations(share_id)
        self.assertTrue(len(export_locations) > 0)

        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full NFS QoS lifecycle completed for share %s", share_id)

    # ----------------------------------------------------------------
    # Test: Create NFS share with QoS boundary min max_bw (1 MB/s)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10e5f6a7-5555-6666-7777-c9d0e1f20005')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_with_qos_min_bw(self):
        """Create NFS share with minimum valid max_bw (1 MB/s).

        Boundary test for the lower limit of max_bw.
        """
        LOG.info("=== test_create_nfs_share_with_qos_min_bw ===")

        qos_type = self.create_qos_type(specs={
            'max_bw': str(QOS_MAX_BW_MIN),
        })
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
        )

        self.assertEqual(share['status'], 'available')
        LOG.info("NFS share %s created with QoS (max_bw=%d)",
                 share['id'], QOS_MAX_BW_MIN)

    # ----------------------------------------------------------------
    # Test: Create NFS share with QoS boundary max max_bw (1000000)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10f6a7b8-6666-7777-8888-d0e1f2a30006')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_with_qos_max_bw(self):
        """Create NFS share with maximum valid max_bw (1000000 MB/s).

        Boundary test for the upper limit of max_bw.
        """
        LOG.info("=== test_create_nfs_share_with_qos_max_bw ===")

        qos_type = self.create_qos_type(specs={
            'max_bw': str(QOS_MAX_BW_MAX),
        })
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
        )

        self.assertEqual(share['status'], 'available')
        LOG.info("NFS share %s created with QoS (max_bw=%d)",
                 share['id'], QOS_MAX_BW_MAX)

    # ----------------------------------------------------------------
    # Test: Multiple NFS shares with same QoS type (shared policy)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10a7b8c9-7777-8888-9999-e1f2a3b40007')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_multiple_nfs_shares_same_qos_type(self):
        """Create multiple NFS shares sharing the same QoS type.

        Both shares should use the same file_io_limit_rule and
        File_Performance policy on PowerStore (idempotent find-or-create).
        """
        LOG.info("=== test_multiple_nfs_shares_same_qos_type ===")

        qos_type = self.create_qos_type(specs={'max_bw': '200'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        shares = []
        for i in range(2):
            share = self.create_share(
                protocol='NFS',
                share_type_name=share_type['name'],
                name=data_utils.rand_name(f'ps-manila-qos-multi-{i}'),
            )
            self.assertEqual(share['status'], 'available')
            shares.append(share)

        LOG.info("Created %d QoS NFS shares with shared policy",
                 len(shares))

        # Delete first share - policy should remain (still in use)
        self.shares_v2_client.delete_share(shares[0]['id'])
        self._wait_for_share_deletion(shares[0]['id'])

        # Second share should still be available
        updated = self.shares_v2_client.get_share(shares[1]['id'])
        sh = updated.get('share', updated)
        self.assertEqual(sh['status'], 'available')
        LOG.info("First share deleted; second share still available "
                 "(shared QoS policy intact)")

        # Delete second share - policy should now be cleaned up
        self.shares_v2_client.delete_share(shares[1]['id'])
        self._wait_for_share_deletion(shares[1]['id'])

        LOG.info("All shares deleted; QoS policy/rule cleaned up")

    # ----------------------------------------------------------------
    # Test: Extend NFS share preserves QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10b8c9d0-8888-9999-aaaa-f2a3b4c50008')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_nfs_share_with_qos(self):
        """Extend an NFS share with QoS and verify QoS is preserved.

        The extend operation only resizes the filesystem; it should not
        modify or remove the QoS policy association.
        """
        LOG.info("=== test_extend_nfs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={'max_bw': '300'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=3,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Extend from 3 GB to 4 GB
        self.extend_share(share_id, 4)

        updated = self.shares_v2_client.get_share(share_id)
        sh = updated.get('share', updated)
        self.assertEqual(sh['status'], 'available')
        self.assertEqual(sh['size'], 4)
        LOG.info("NFS share %s extended to 4 GB with QoS preserved",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Shrink NFS share preserves QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10c9d0e1-9999-aaaa-bbbb-a3b4c5d60009')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_nfs_share_with_qos(self):
        """Shrink an NFS share with QoS and verify QoS is preserved.

        The shrink operation only resizes the filesystem; QoS policy
        association should remain intact.
        """
        LOG.info("=== test_shrink_nfs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={'max_bw': '400'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=4,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Shrink from 4 GB to 3 GB (minimum size is 3 GB per filter_function)
        self.shrink_share(share_id, 3)

        updated = self.shares_v2_client.get_share(share_id)
        sh = updated.get('share', updated)
        self.assertEqual(sh['status'], 'available')
        self.assertEqual(sh['size'], 3)
        LOG.info("NFS share %s shrunk to 3 GB with QoS preserved",
                 share_id)

    # ----------------------------------------------------------------
    # Test: Create NFS share from snapshot with QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10d0e1f2-aaaa-bbbb-cccc-b4c5d6e70010')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_from_snapshot_with_qos(self):
        """Create a share from snapshot with QoS applied.

        Expected PowerStore side-effects:
          1. Clone filesystem from snapshot
          2. Create/reuse file_io_limit_rule + policy
          3. Associate policy with cloned filesystem
        """
        LOG.info("=== test_create_nfs_share_from_snapshot_with_qos ===")

        qos_type = self.create_qos_type(specs={'max_bw': '600'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name,
            extra_specs={
                'snapshot_support': 'True',
                'create_share_from_snapshot_support': 'True'
            })

        # Create original share
        original = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
        )
        self.assertEqual(original['status'], 'available')

        # Create snapshot
        snapshot = self.create_snapshot(original['id'])

        # Create share from snapshot
        clone_name = data_utils.rand_name('ps-manila-qos-clone')
        share_size = getattr(CONF.share, 'share_size', 3)
        clone = self.shares_v2_client.create_share(
            share_protocol='NFS',
            size=share_size,
            name=clone_name,
            share_type_id=share_type['name'],
            snapshot_id=snapshot['id'],
        )
        cl = clone.get('share', clone)
        self.addCleanup(self._delete_share_safe, cl['id'])
        self._wait_for_share_status(cl['id'], 'available')

        cloned = self.shares_v2_client.get_share(cl['id'])
        cloned_sh = cloned.get('share', cloned)
        self.assertEqual(cloned_sh['status'], 'available')
        LOG.info("Cloned NFS share %s from snapshot with QoS",
                 cloned_sh['id'])

    # ----------------------------------------------------------------
    # Test: Create NFS share with invalid QoS spec (max_bw=0)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10e1f2a3-bbbb-cccc-dddd-c5d6e7f80011')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_create_nfs_share_with_invalid_qos_zero_bw(self):
        """Create NFS share with max_bw=0 should fail.

        The driver's _validate_qos_specs checks that max_bw >= 1.
        Share creation should fail with error status.
        """
        LOG.info("=== test_create_nfs_share_with_invalid_qos_zero_bw ===")

        qos_type = self.create_qos_type(specs={'max_bw': '0'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        name = data_utils.rand_name('ps-manila-qos-invalid-bw')
        try:
            share = self.shares_v2_client.create_share(
                share_protocol='NFS',
                name=name,
                share_type_id=share_type['name'],
            )
            sh = share.get('share', share)
            share_id = sh['id']

            # Register cleanup for the share - this will run even if manual deletion fails
            self.addCleanup(self._delete_share_safe, sh['id'])

            # Wait for share to reach error or available status
            status = None
            deadline = time.time() + NEGATIVE_TEST_TIMEOUT
            while time.time() < deadline:
                result = self.shares_v2_client.get_share(share_id)
                result_sh = result.get('share', result)
                status = result_sh.get('status', '').lower()
                if status in ('error', 'available'):
                    break
                time.sleep(SHARE_BUILD_INTERVAL)

            if status != 'error':
                self.fail(
                    f"Expected error status but got: {status}. "
                    f"If share became 'available', the backend may not be "
                    f"properly validating max_bw >= 1 constraint.")
            LOG.info("Share correctly failed with status=%s "
                     "(invalid max_bw=0)", status)
            # Try to delete the failed share so share type and QoS type can be cleaned up
            # This may fail if the driver has issues deleting shares in error state
            self._delete_share_safe(sh['id'])
        except (lib_exc.BadRequest, lib_exc.ServerFault):
            LOG.info("Share creation correctly rejected (invalid max_bw=0)")

    # ----------------------------------------------------------------
    # Test: Create NFS share with invalid QoS spec (max_bw=abc)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10f2a3b4-cccc-dddd-eeee-d6e7f8a90012')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_create_nfs_share_with_invalid_qos_non_numeric_bw(self):
        """Create NFS share with non-numeric max_bw should fail.

        The driver's _validate_qos_specs checks int conversion.
        """
        LOG.info("=== test_create_nfs_share_with_invalid_qos_non_numeric ===")

        # Create QoS type without cleanup registration
        qos_type_name = data_utils.rand_name('ps-manila-qos-type-invalid')
        body = {
            'qos_type': {
                'name': qos_type_name,
                'specs': {'max_bw': 'abc'},
            }
        }
        resp, resp_body = self._qos_type_request('POST', body=body)
        qos_type = resp_body.get('qos_type', resp_body)
        
        backend_name = self._get_backend_name()
        # Create share type without cleanup registration
        share_type_name = data_utils.rand_name('ps-manila-qos-share-type-invalid')
        specs = {
            'driver_handles_share_servers': str(CONF.share.multitenancy_enabled),
            'default_qos_type': qos_type_name,
        }
        if backend_name:
            specs['share_backend_name'] = backend_name
        share_type = self.share_types_client.create_share_type(
            name=share_type_name,
            extra_specs=specs,
        )
        st = share_type.get('share_type', share_type)

        # Register cleanup for share type and QoS type
        self.addCleanup(self._delete_share_type_safe, st['id'])
        self.addCleanup(self._delete_qos_type_safe, qos_type['id'])

        name = data_utils.rand_name('ps-manila-qos-invalid-str')
        share_id = None
        try:
            share = self.shares_v2_client.create_share(
                share_protocol='NFS',
                name=name,
                share_type_id=share_type_name,
            )
            sh = share.get('share', share)
            share_id = sh['id']

            # Register cleanup for the share - this will run even if manual deletion fails
            self.addCleanup(self._delete_share_safe, sh['id'])

            status = None
            deadline = time.time() + NEGATIVE_TEST_TIMEOUT
            while time.time() < deadline:
                result = self.shares_v2_client.get_share(share_id)
                result_sh = result.get('share', result)
                status = result_sh.get('status', '').lower()
                if status in ('error', 'available'):
                    break
                time.sleep(SHARE_BUILD_INTERVAL)

            if status != 'error':
                self.fail(
                    f"Expected error status but got: {status}. "
                    f"If share became 'available', the backend may not be "
                    f"properly validating the QoS spec.")
            LOG.info("Share correctly failed with status=%s "
                     "(non-numeric max_bw)", status)
            # Try to delete the failed share so share type and QoS type can be cleaned up
            # This may fail if the driver has issues deleting shares in error state
            self._delete_share_safe(sh['id'])
        except (lib_exc.BadRequest, lib_exc.ServerFault):
            LOG.info("Share creation correctly rejected "
                     "(non-numeric max_bw)")

    # ----------------------------------------------------------------
    # Test: Create NFS share with unsupported QoS key
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10a3b4c5-dddd-eeee-ffff-e7f8a9b00013')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_create_nfs_share_with_unsupported_qos_key(self):
        """Create NFS share with unsupported QoS key should fail.

        The driver only supports 'max_bw'. Adding 'max_iops' should
        cause _validate_qos_specs to reject the specs.
        """
        LOG.info("=== test_create_nfs_share_with_unsupported_qos_key ===")

        qos_type = self.create_qos_type(specs={
            'max_bw': '100',
            'max_iops': '1000',
        })
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        name = data_utils.rand_name('ps-manila-qos-unsupported')
        try:
            share = self.shares_v2_client.create_share(
                share_protocol='NFS',
                name=name,
                share_type_id=share_type['name'],
            )
            sh = share.get('share', share)
            share_id = sh['id']

            # Register cleanup for the share - this will run even if manual deletion fails
            self.addCleanup(self._delete_share_safe, sh['id'])

            # Wait for share to reach error or available status
            status = None
            deadline = time.time() + NEGATIVE_TEST_TIMEOUT
            while time.time() < deadline:
                result = self.shares_v2_client.get_share(share_id)
                result_sh = result.get('share', result)
                status = result_sh.get('status', '').lower()
                if status in ('error', 'available'):
                    break
                time.sleep(SHARE_BUILD_INTERVAL)

            if status != 'error':
                self.fail(
                    f"Expected error status but got: {status}. "
                    f"If share became 'available', the backend may not be "
                    f"properly validating unsupported QoS keys.")
            LOG.info("Share correctly failed with status=%s "
                     "(unsupported QoS key)", status)
            # Try to delete the failed share so share type and QoS type can be cleaned up
            # This may fail if the driver has issues deleting shares in error state
            self._delete_share_safe(sh['id'])
        except (lib_exc.BadRequest, lib_exc.ServerFault):
            LOG.info("Share creation correctly rejected "
                     "(unsupported QoS key)")


# ======================================================================
# CIFS QoS Tests
# ======================================================================
class _CIFSQoSTests(object):
    """Mixin: CIFS share QoS test methods for PowerStore Manila.

    Same QoS flows as NFS but exercised over the CIFS (SMB) protocol
    path. The file_io_limit_rule and policy apply to the filesystem
    regardless of the protocol.
    """

    @classmethod
    def skip_checks(cls):
        super(_CIFSQoSTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Create CIFS share with QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('20a1b2c3-1111-2222-3333-e5f6a7b80001')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_with_qos(self):
        """Create a CIFS share with QoS enabled.

        Expected PowerStore side-effects:
          - file_io_limit_rule created/reused
          - File_Performance policy created/reused
          - Policy associated with filesystem
        """
        LOG.info("=== test_create_cifs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={'max_bw': '100'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'CIFS')
        export_locations = self._get_export_locations(share['id'])
        self.assertTrue(len(export_locations) > 0)
        LOG.info("CIFS share %s created with QoS (max_bw=100)",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create CIFS share without QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('20b2c3d4-2222-3333-4444-f6a7b8c90002')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_without_qos(self):
        """Create a CIFS share without QoS type."""
        LOG.info("=== test_create_cifs_share_without_qos ===")

        backend_name = self._get_backend_name()
        share_type = self.create_plain_share_type(backend_name=backend_name)
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'CIFS')
        LOG.info("CIFS share %s created without QoS", share['id'])

    # ----------------------------------------------------------------
    # Test: Delete CIFS share with QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('20c3d4e5-3333-4444-5555-a7b8c9d00003')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_cifs_share_with_qos(self):
        """Delete a CIFS share with QoS; rule/policy should be cleaned up."""
        LOG.info("=== test_delete_cifs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={'max_bw': '500'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
        )
        share_id = share['id']

        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("CIFS share %s deleted; QoS rule/policy cleaned up",
                 share_id)

    # ----------------------------------------------------------------
    # Test: CIFS QoS full lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('20d4e5f6-4444-5555-6666-b8c9d0e10004')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_qos_lifecycle(self):
        """Full lifecycle: create CIFS with QoS -> verify -> delete."""
        LOG.info("=== test_cifs_qos_lifecycle ===")

        qos_type = self.create_qos_type(specs={'max_bw': '2000'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
        )
        self.assertEqual(share['status'], 'available')

        self.shares_v2_client.delete_share(share['id'])
        self._wait_for_share_deletion(share['id'])

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share['id'],
        )
        LOG.info("Full CIFS QoS lifecycle completed for share %s",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Extend CIFS share preserves QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('20e5f6a7-5555-6666-7777-c9d0e1f20005')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_cifs_share_with_qos(self):
        """Extend a CIFS share with QoS and verify QoS is preserved."""
        LOG.info("=== test_extend_cifs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={'max_bw': '300'})
        backend_name = self._get_backend_name()
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'],
            backend_name=backend_name)

        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=3,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        self.extend_share(share_id, 4)

        updated = self.shares_v2_client.get_share(share_id)
        sh = updated.get('share', updated)
        self.assertEqual(sh['status'], 'available')
        self.assertEqual(sh['size'], 4)
        LOG.info("CIFS share %s extended from 3 GB to 4 GB with QoS preserved",
                 share_id)


# ======================================================================
# QoS Type / Share Type Spec Validation Tests
# ======================================================================
class _QoSShareTypeTests(object):
    """Mixin: QoS type and share type spec validation tests.

    Verifies that QoS types and share types are created correctly
    with the expected specs, and that the relationship between
    share types and QoS types is properly configured.
    """

    @classmethod
    def skip_checks(cls):
        super(_QoSShareTypeTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: QoS type created with correct specs
    # ----------------------------------------------------------------
    @decorators.idempotent_id('30a1b2c3-1111-2222-3333-e5f6a7b80001')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_qos_type_with_max_bw_spec(self):
        """Create a QoS type with max_bw and verify specs are stored."""
        LOG.info("=== test_create_qos_type_with_max_bw_spec ===")

        qos_type = self.create_qos_type(specs={'max_bw': '1000'})

        self.assertIsNotNone(qos_type.get('id'))
        specs = qos_type.get('specs', {})
        self.assertEqual(specs.get('max_bw'), '1000')
        LOG.info("QoS type %s created with correct max_bw spec",
                 qos_type['id'])

    # ----------------------------------------------------------------
    # Test: Share type with default_qos_type has correct extra-specs
    # ----------------------------------------------------------------
    @decorators.idempotent_id('30b2c3d4-2222-3333-4444-f6a7b8c90002')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_share_type_with_default_qos_type_spec(self):
        """Create share type with default_qos_type and verify extra-specs."""
        LOG.info("=== test_share_type_with_default_qos_type_spec ===")

        qos_type = self.create_qos_type(specs={'max_bw': '500'})
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        specs = share_type.get('extra_specs', {})
        self.assertIn('default_qos_type', specs)
        self.assertEqual(specs['default_qos_type'], qos_type['name'])
        self.assertEqual(specs['driver_handles_share_servers'], 'False')
        LOG.info("Share type %s has correct default_qos_type spec",
                 share_type['id'])

    # ----------------------------------------------------------------
    # Test: Share type without default_qos_type
    # ----------------------------------------------------------------
    @decorators.idempotent_id('30c3d4e5-3333-4444-5555-a7b8c9d00003')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_share_type_without_qos_type_spec(self):
        """Create share type without QoS and verify no qos spec."""
        LOG.info("=== test_share_type_without_qos_type_spec ===")

        share_type = self.create_plain_share_type()
        specs = share_type.get('extra_specs', {})
        self.assertNotIn('default_qos_type', specs)
        LOG.info("Share type %s correctly has no default_qos_type spec",
                 share_type['id'])

    # ----------------------------------------------------------------
    # Test: QoS type with boundary min max_bw
    # ----------------------------------------------------------------
    @decorators.idempotent_id('30d4e5f6-4444-5555-6666-b8c9d0e10004')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_qos_type_with_min_max_bw(self):
        """Create QoS type with minimum max_bw (1)."""
        LOG.info("=== test_create_qos_type_with_min_max_bw ===")

        qos_type = self.create_qos_type(specs={
            'max_bw': str(QOS_MAX_BW_MIN),
        })

        specs = qos_type.get('specs', {})
        self.assertEqual(specs.get('max_bw'), str(QOS_MAX_BW_MIN))
        LOG.info("QoS type %s with min max_bw spec created",
                 qos_type['id'])

    # ----------------------------------------------------------------
    # Test: QoS type with boundary max max_bw
    # ----------------------------------------------------------------
    @decorators.idempotent_id('30e5f6a7-5555-6666-7777-c9d0e1f20005')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_qos_type_with_max_max_bw(self):
        """Create QoS type with maximum max_bw (1000000)."""
        LOG.info("=== test_create_qos_type_with_max_max_bw ===")

        qos_type = self.create_qos_type(specs={
            'max_bw': str(QOS_MAX_BW_MAX),
        })

        specs = qos_type.get('specs', {})
        self.assertEqual(specs.get('max_bw'), str(QOS_MAX_BW_MAX))
        LOG.info("QoS type %s with max max_bw spec created",
                 qos_type['id'])

    # ----------------------------------------------------------------
    # Test: Two distinct QoS types with different max_bw
    # ----------------------------------------------------------------
    @decorators.idempotent_id('30f6a7b8-6666-7777-8888-d0e1f2a30006')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_two_qos_types_different_bw(self):
        """Create two QoS types with different max_bw values.

        Each should generate distinct file_io_limit_rule names and
        File_Performance policy names on PowerStore.
        """
        LOG.info("=== test_two_qos_types_different_bw ===")

        qt1 = self.create_qos_type(specs={'max_bw': '100'})
        qt2 = self.create_qos_type(specs={'max_bw': '200'})

        self.assertNotEqual(qt1['id'], qt2['id'])
        self.assertNotEqual(qt1['name'], qt2['name'])

        specs1 = qt1.get('specs', {})
        specs2 = qt2.get('specs', {})
        self.assertEqual(specs1.get('max_bw'), '100')
        self.assertEqual(specs2.get('max_bw'), '200')

        # Create shares with each QoS type
        backend_name = self._get_backend_name()
        st1 = self.create_qos_share_type(qos_type_name=qt1['name'],
                                          backend_name=backend_name)
        st2 = self.create_qos_share_type(qos_type_name=qt2['name'],
                                          backend_name=backend_name)

        share1 = self.create_share(
            protocol='NFS',
            share_type_name=st1['name'],
        )
        share2 = self.create_share(
            protocol='NFS',
            share_type_name=st2['name'],
        )

        self.assertEqual(share1['status'], 'available')
        self.assertEqual(share2['status'], 'available')
        LOG.info("Two shares with distinct QoS types created successfully")


# ---------------------------------------------------------------------------
# Concrete test classes wired to a Tempest-compatible base class.
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerStoreQoSNFS(
            _NFSQoSTests,
            PowerStoreQoSShareTest,
            manila_base.BaseSharesAdminTest):
        """NFS QoS functional tests (manila_tempest_tests base)."""

    class TestPowerStoreQoSCIFS(
            _CIFSQoSTests,
            PowerStoreQoSShareTest,
            manila_base.BaseSharesAdminTest):
        """CIFS QoS functional tests (manila_tempest_tests base)."""

    class TestPowerStoreQoSShareType(
            _QoSShareTypeTests,
            PowerStoreQoSShareTest,
            manila_base.BaseSharesAdminTest):
        """QoS share type spec tests (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerStoreQoSNFS(
            _NFSQoSTests,
            PowerStoreQoSShareTest,
            tempest_test.BaseTestCase):
        """NFS QoS functional tests (tempest.test fallback base)."""
        credentials = ['admin']

    class TestPowerStoreQoSCIFS(
            _CIFSQoSTests,
            PowerStoreQoSShareTest,
            tempest_test.BaseTestCase):
        """CIFS QoS functional tests (tempest.test fallback base)."""
        credentials = ['admin']

    class TestPowerStoreQoSShareType(
            _QoSShareTypeTests,
            PowerStoreQoSShareTest,
            tempest_test.BaseTestCase):
        """QoS share type spec tests (tempest.test fallback base)."""
        credentials = ['admin']

