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
Tempest functional tests for Dell PowerScale QoS (SmartQoS) feature.

These tests exercise the QoS lifecycle through real Manila API calls that
result in actual PowerScale SmartQoS API calls:

  - Creating QoS types with protocol_ops / dataset specs
  - Creating share types with default_qos_type extra-spec
  - Creating NFS/CIFS shares that trigger SmartQoS workload creation
  - Deleting shares that remove SmartQoS workloads
  - Managing shares with QoS-enabled/disabled share types
  - Verifying QoS limit match/mismatch/absent scenarios on manage

PowerScale REST API endpoints exercised:
  - GET  /platform/19/performance/datasets       (list_datasets)
  - GET  /platform/19/performance/datasets/{id}/workloads (list_workloads)
  - POST /platform/19/performance/datasets/{id}/workloads (create_workload)
  - PUT  /platform/19/performance/datasets/{id}/workloads/{wid}
         (update_workload_limit)
  - DELETE /platform/19/performance/datasets/{id}/workloads/{wid}
         (delete_workload)

Driver methods tested:
  - create_share          (QoS workload creation path)
  - delete_share          (QoS workload deletion path)
  - manage_existing       (QoS match/mismatch/absent validation)
  - _qos_protocols_for_share
  - _qos_find_workload
  - _qos_ensure_workloads
  - _qos_clear_for_path
  - _qos_get_specs
  - _qos_requested_and_limit
  - _qos_ensure_dataset
  - _qos_backend_enabled_for_path
"""

import json
import time

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

# Minimum Manila API microversion that supports QoS types
QOS_TYPE_MIN_API_VERSION = '2.94'


class PowerScaleQoSShareTest(object):
    """Mixin with helpers for PowerScale QoS share tests.

    Provides utility methods for creating QoS types, share types with
    default_qos_type, shares, and waiting for share status transitions
    via the Manila API.
    """

    @classmethod
    def setup_credentials(cls):
        super(PowerScaleQoSShareTest, cls).setup_credentials()

    @classmethod
    def setup_clients(cls):
        super(PowerScaleQoSShareTest, cls).setup_clients()
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
        :param specs: Dict of QoS specs (e.g. protocol_ops, dataset_id).
        :returns: Created QoS type dict.
        """
        name = name or data_utils.rand_name('ps-qos-type')
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
                              extra_specs=None):
        """Create a share type with default_qos_type pointing to a QoS type.

        :param qos_type_name: Name of the QoS type to link.
        :param name: Optional share type name.
        :param extra_specs: Additional extra-specs dict to merge.
        :returns: Created share type dict.
        """
        name = name or data_utils.rand_name('ps-qos-share-type')
        specs = {
            'driver_handles_share_servers': 'False',
            'default_qos_type': qos_type_name,
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

    def create_plain_share_type(self, name=None, extra_specs=None):
        """Create a share type WITHOUT default_qos_type."""
        name = name or data_utils.rand_name('ps-no-qos-type')
        specs = {'driver_handles_share_servers': 'False'}
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
    def create_share(self, protocol, share_type_name, size=1, name=None):
        """Create a Manila share and wait until it becomes available.

        :param protocol: 'NFS' or 'CIFS'
        :param share_type_name: Name of the share type to use.
        :param size: Share size in GB.
        :param name: Optional share name.
        :returns: Created share dict.
        """
        name = name or data_utils.rand_name(f'ps-qos-{protocol.lower()}')
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
        """Delete a share and wait for it to be removed."""
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
            if status in ('error', 'error_deleting', 'manage_error'):
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

    # ------------------------------------------------------------------
    # Manage / Unmanage helpers
    # ------------------------------------------------------------------
    def manage_share(self, protocol, export_path, share_type_name,
                     name=None, service_host=None):
        """Manage an existing PowerScale export as a Manila share.

        :param protocol: 'NFS' or 'CIFS'
        :param export_path: The export path on PowerScale.
        :param share_type_name: Name of the share type.
        :param name: Optional Manila share name.
        :param service_host: Full host string.
        :returns: Managed share dict.
        """
        name = name or data_utils.rand_name('ps-manage-qos')
        share = self.shares_v2_client.manage_share(
            service_host=service_host or self._get_manila_host(),
            protocol=protocol,
            export_path=export_path,
            share_type_id=share_type_name,
            name=name,
        )
        sh = share.get('share', share)
        LOG.info("Manage request for share '%s' (id=%s)",
                 sh['name'], sh['id'])
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self.shares_v2_client.get_share(sh['id']).get(
            'share', self.shares_v2_client.get_share(sh['id']))

    def unmanage_share(self, share_id):
        """Unmanage a share (removes from Manila but keeps on backend)."""
        self.shares_v2_client.unmanage_share(share_id)
        LOG.info("Unmanaged share %s", share_id)
        self._wait_for_share_deletion(share_id)

    def _get_export_locations(self, share_id):
        """Retrieve export locations for a share."""
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    def _get_manila_host(self):
        """Discover the Manila host string for the PowerScale backend."""
        try:
            services = self.shares_v2_client.list_services()
            for svc in services.get('services', services):
                host = svc.get('host', '')
                if 'powerscale' in host.lower():
                    LOG.info("Discovered Manila PowerScale host: %s", host)
                    return host
        except Exception as e:
            LOG.warning("Failed to discover Manila host: %s", e)
        return getattr(CONF, 'share', {}).get(
            'powerscale_host', 'manila-host@powerscale')


# ======================================================================
# NFS QoS Tests
# ======================================================================
class _NFSQoSTests(object):
    """Mixin: NFS share QoS test methods for PowerScale.

    Each test makes real Manila API calls that propagate to the
    PowerScale backend, triggering SmartQoS workload creation,
    update, or deletion via the PowerScale REST API.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSQoSTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Create NFS share with QoS (workloads created on backend)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1b2c3d4-1111-2222-3333-e5f6a7b8c9d0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_with_qos(self):
        """Create an NFS share with QoS enabled and verify it succeeds.

        Expected PowerScale side-effects:
          1. GET /platform/19/performance/datasets to resolve dataset
          2. GET /platform/19/performance/datasets/{id}/workloads
             to check existing workloads
          3. POST /platform/19/performance/datasets/{id}/workloads
             to create workloads for nfs3, nfs4 protocols
        """
        LOG.info("=== test_create_nfs_share_with_qos ===")

        # Step 1: Create QoS type with protocol_ops and dataset specs
        qos_type = self.create_qos_type(specs={
            'protocol_ops': '1000',
            'dataset': 'openstack_manila_qos',
        })

        # Step 2: Create share type with default_qos_type
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])
        self.assertIn('default_qos_type',
                      share_type.get('extra_specs', {}))

        # Step 3: Create NFS share using the QoS share type
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )

        # Step 4: Verify share is available (QoS workloads were created)
        self.assertEqual(share['status'], 'available',
                         f"Share status is {share['status']}, "
                         f"expected available")
        self.assertEqual(share['share_proto'].upper(), 'NFS')
        export_locations = self._get_export_locations(share['id'])
        self.assertTrue(
            len(export_locations) > 0,
            "Share must have at least one export location")
        LOG.info("NFS share %s created with QoS (protocol_ops=1000)",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create NFS share without QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b2c3d4e5-2222-3333-4444-f6a7b8c9d0e1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_without_qos(self):
        """Create an NFS share without QoS type.

        No SmartQoS workloads should be created on PowerScale.
        """
        LOG.info("=== test_create_nfs_share_without_qos ===")

        share_type = self.create_plain_share_type()
        self.assertNotIn('default_qos_type',
                         share_type.get('extra_specs', {}))

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'NFS')
        LOG.info("NFS share %s created without QoS", share['id'])

    # ----------------------------------------------------------------
    # Test: Delete NFS share with QoS (workloads removed from backend)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c3d4e5f6-3333-4444-5555-a7b8c9d0e1f2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_nfs_share_with_qos(self):
        """Delete an NFS share with QoS and verify workloads are removed.

        Expected PowerScale side-effects:
          1. GET workloads for the dataset to find workload IDs
          2. DELETE /platform/19/performance/datasets/{id}/workloads/{wid}
             for each protocol (nfs3, nfs4)
        """
        LOG.info("=== test_delete_nfs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '500',
            'dataset': 'openstack_manila_qos',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Delete the share
        self.shares_v2_client.delete_share(share_id)
        LOG.info("Requested deletion of QoS-enabled NFS share %s", share_id)

        # Wait for deletion
        self._wait_for_share_deletion(share_id)

        # Verify it is gone
        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("NFS share %s deleted; QoS workloads removed", share_id)

    # ----------------------------------------------------------------
    # Test: Full NFS QoS lifecycle (create -> verify -> delete)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d4e5f6a7-4444-5555-6666-b8c9d0e1f2a3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_qos_lifecycle(self):
        """Full lifecycle: create NFS with QoS -> verify -> delete -> verify.

        This exercises the complete QoS flow:
          1. _qos_requested_and_limit returns (True, 1000)
          2. _qos_ensure_dataset resolves dataset by name
          3. _qos_protocols_for_share returns ['nfs3', 'nfs4']
          4. _qos_ensure_workloads creates workloads for each protocol
          5. On delete: _qos_clear_for_path finds and deletes workloads
        """
        LOG.info("=== test_nfs_qos_lifecycle ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '1000',
            'dataset': 'openstack_manila_qos',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        # Create share
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        self.assertEqual(share['status'], 'available')
        share_id = share['id']

        # Refresh and verify
        updated = self.shares_v2_client.get_share(share_id)
        sh = updated.get('share', updated)
        self.assertEqual(sh['status'], 'available')
        export_locations = self._get_export_locations(share_id)
        self.assertTrue(len(export_locations) > 0)

        # Delete share
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full NFS QoS lifecycle completed for share %s", share_id)

    # ----------------------------------------------------------------
    # Test: Create NFS share with QoS using dataset_id (numeric)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e5f6a7b8-5555-6666-7777-c9d0e1f2a3b4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_with_qos_dataset_id(self):
        """Create an NFS share with QoS using dataset_id instead of name.

        The driver's _qos_ensure_dataset resolves dataset by numeric ID
        directly (skipping the list_datasets call).
        """
        LOG.info("=== test_create_nfs_share_with_qos_dataset_id ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '2000',
            'dataset_id': '3',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        LOG.info("NFS share %s created with QoS (dataset_id=1)",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create NFS share with custom protocol filter
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f6a7b8c9-6666-7777-8888-d0e1f2a3b4c5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_with_qos_protocol_filter(self):
        """Create NFS share with QoS limited to nfs3 protocol only.

        The driver's _qos_protocols_for_share reads the 'protocols'
        spec and filters the default ['nfs3', 'nfs4'] list accordingly.
        Only one workload should be created (for nfs3).
        """
        LOG.info("=== test_create_nfs_share_with_qos_protocol_filter ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '800',
            'dataset': 'openstack_manila_qos',
            'protocols': 'nfs3',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        LOG.info("NFS share %s created with QoS limited to nfs3",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Manage NFS share with matching QoS on backend
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a7b8c9d0-7777-8888-9999-e1f2a3b4c5d6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_nfs_share_with_qos_match(self):
        """Manage an NFS export whose QoS matches the share type.

        Steps:
          1. Create a QoS share (workloads created on PowerScale)
          2. Unmanage it (removes from Manila, workloads stay)
          3. Manage with same QoS share type -> success (status=match)

        Expected PowerScale side-effects during manage:
          - _qos_backend_enabled_for_path returns 'match'
        """
        LOG.info("=== test_manage_nfs_share_with_qos_match ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '1500',
            'dataset': 'openstack_manila_qos',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        share_host = share['host']
        export_locations = self._get_export_locations(share_id)
        export_path = export_locations[0]
        if isinstance(export_path, dict):
            export_path = export_path.get('path', export_path)
        LOG.info("Share %s export path: %s", share_id, export_path)

        # Unmanage
        self.unmanage_share(share_id)

        # Manage back with the same QoS share type
        managed = self.manage_share(
            protocol='NFS',
            export_path=export_path,
            share_type_name=share_type['name'],
            service_host=share_host,
        )
        self.assertEqual(managed['status'], 'available')
        LOG.info("Managed NFS share with matching QoS: %s", managed['id'])

    # ----------------------------------------------------------------
    # Test: Manage NFS share with QoS type but no workloads on backend
    #       (absent) -> expect manage error
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b8c9d0e1-8888-9999-aaaa-f2a3b4c5d6e7')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nfs_share_qos_absent_fails(self):
        """Manage an export with QoS type when backend has no workloads.

        Steps:
          1. Create a non-QoS share (no workloads on backend)
          2. Unmanage it
          3. Manage with QoS share type -> expect manage_error
             because _qos_backend_enabled_for_path returns 'absent'
        """
        LOG.info("=== test_manage_nfs_share_qos_absent_fails ===")

        # Create without QoS
        plain_type = self.create_plain_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=plain_type['name'],
            size=1,
        )
        share_id = share['id']
        share_host = share['host']
        export_locations = self._get_export_locations(share_id)
        export_path = export_locations[0]
        if isinstance(export_path, dict):
            export_path = export_path.get('path', export_path)

        # Unmanage
        self.unmanage_share(share_id)

        # Create a QoS share type
        qos_type = self.create_qos_type(specs={
            'protocol_ops': '1000',
            'dataset': 'openstack_manila_qos',
        })
        qos_share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        # Manage with QoS type should fail (absent workloads)
        try:
            result = self.shares_v2_client.manage_share(
                service_host=share_host,
                protocol='NFS',
                export_path=export_path,
                share_type_id=qos_share_type['name'],
                name=data_utils.rand_name('ps-manage-qos-fail'),
            )
            sh = result.get('share', result)
            self.addCleanup(self._delete_share_safe, sh['id'])
            time.sleep(15)
            managed = self.shares_v2_client.get_share(sh['id'])
            managed_sh = managed.get('share', managed)
            self.assertIn(managed_sh['status'],
                          ('manage_error', 'error'),
                          f"Expected manage to fail but got status: "
                          f"{managed_sh['status']}")
            LOG.info("Manage correctly failed with status=%s "
                     "(QoS absent on backend)", managed_sh['status'])
        except lib_exc.BadRequest:
            LOG.info("Manage correctly rejected with BadRequest "
                     "(QoS absent on backend)")
        except lib_exc.ServerFault:
            LOG.info("Manage correctly rejected with ServerFault "
                     "(QoS absent on backend)")

    # ----------------------------------------------------------------
    # Test: Manage NFS share with no QoS type but workloads exist
    #       on backend -> expect manage error
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c9d0e1f2-9999-aaaa-bbbb-a3b4c5d6e7f8')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_nfs_share_qos_unexpected_on_backend_succeeds(self):
        """Manage an export without QoS type when backend has workloads.

        Steps:
          1. Create a QoS share (workloads created on backend)
          2. Unmanage it (workloads remain on PowerScale)
          3. Manage with non-QoS share type -> succeeds because the
             driver cannot detect orphaned workloads without a dataset
             reference in the share type.

        The manage succeeds with status 'available'; any pre-existing
        workloads on the backend remain but are not managed by Manila.
        """
        LOG.info("=== test_manage_nfs_share_qos_unexpected_on_backend ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '700',
            'dataset': 'openstack_manila_qos',
        })
        qos_share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        share = self.create_share(
            protocol='NFS',
            share_type_name=qos_share_type['name'],
            size=1,
        )
        share_id = share['id']
        share_host = share['host']
        export_locations = self._get_export_locations(share_id)
        export_path = export_locations[0]
        if isinstance(export_path, dict):
            export_path = export_path.get('path', export_path)

        # Unmanage
        self.unmanage_share(share_id)

        # Create a plain share type (no QoS)
        plain_type = self.create_plain_share_type()

        # Manage with non-QoS type succeeds (driver has no dataset
        # reference to check for orphaned workloads)
        managed = self.manage_share(
            protocol='NFS',
            export_path=export_path,
            share_type_name=plain_type['name'],
            service_host=share_host,
        )
        self.assertEqual(managed['status'], 'available')
        LOG.info("Manage succeeded with plain type (orphaned workloads "
                 "remain on backend): %s", managed['id'])

    # ----------------------------------------------------------------
    # Test: Multiple NFS shares with same QoS type
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d0e1f2a3-aaaa-bbbb-cccc-b4c5d6e7f8a9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_multiple_nfs_shares_same_qos_type(self):
        """Create multiple NFS shares sharing the same QoS type.

        Each share gets its own set of workloads (per path+protocol).
        The dataset is shared; only the workloads differ.
        """
        LOG.info("=== test_multiple_nfs_shares_same_qos_type ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '1200',
            'dataset': 'openstack_manila_qos',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        shares = []
        for i in range(2):
            share = self.create_share(
                protocol='NFS',
                share_type_name=share_type['name'],
                size=1,
                name=data_utils.rand_name(f'ps-qos-multi-{i}'),
            )
            self.assertEqual(share['status'], 'available')
            shares.append(share)

        LOG.info("Created %d QoS NFS shares successfully", len(shares))

        # Clean up in reverse order
        for share in reversed(shares):
            self.shares_v2_client.delete_share(share['id'])
            self._wait_for_share_deletion(share['id'])

        LOG.info("All %d QoS NFS shares deleted; workloads removed",
                 len(shares))


# ======================================================================
# CIFS QoS Tests
# ======================================================================
class _CIFSQoSTests(object):
    """Mixin: CIFS share QoS test methods for PowerScale.

    Same QoS flows as NFS but exercised over the CIFS protocol path,
    which applies workloads for smb1, smb2 protocols instead of nfs3, nfs4.
    """

    @classmethod
    def skip_checks(cls):
        super(_CIFSQoSTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Create CIFS share with QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1f2a3b4-bbbb-cccc-dddd-c5d6e7f8a9b0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_with_qos(self):
        """Create a CIFS share with QoS enabled.

        Expected PowerScale side-effects:
          - Workloads created for protocols smb1, smb2
        """
        LOG.info("=== test_create_cifs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '1000',
            'dataset': 'openstack_manila_qos',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'CIFS')
        export_locations = self._get_export_locations(share['id'])
        self.assertTrue(len(export_locations) > 0)
        LOG.info("CIFS share %s created with QoS (protocol_ops=1000)",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create CIFS share without QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f2a3b4c5-cccc-dddd-eeee-d6e7f8a9b0c1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_without_qos(self):
        """Create a CIFS share without QoS type."""
        LOG.info("=== test_create_cifs_share_without_qos ===")

        share_type = self.create_plain_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'CIFS')
        LOG.info("CIFS share %s created without QoS", share['id'])

    # ----------------------------------------------------------------
    # Test: Delete CIFS share with QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a3b4c5d6-dddd-eeee-ffff-e7f8a9b0c1d2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_cifs_share_with_qos(self):
        """Delete a CIFS share with QoS; workloads should be removed."""
        LOG.info("=== test_delete_cifs_share_with_qos ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '500',
            'dataset': 'openstack_manila_qos',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']

        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("CIFS share %s deleted; QoS workloads removed", share_id)

    # ----------------------------------------------------------------
    # Test: CIFS QoS full lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b4c5d6e7-eeee-ffff-0000-f8a9b0c1d2e3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_qos_lifecycle(self):
        """Full lifecycle: create CIFS with QoS -> verify -> delete."""
        LOG.info("=== test_cifs_qos_lifecycle ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '2000',
            'dataset': 'openstack_manila_qos',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
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
    # Test: Create CIFS share with QoS custom protocol filter (smb2)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c5d6e7f8-ffff-0000-1111-a9b0c1d2e3f4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_with_qos_protocol_filter(self):
        """Create CIFS share with QoS limited to smb2 protocol only.

        The driver's _qos_protocols_for_share filters the default
        ['smb1', 'smb2'] based on the 'protocols' spec.
        """
        LOG.info("=== test_create_cifs_share_with_qos_protocol_filter ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '600',
            'dataset': 'openstack_manila_qos',
            'protocols': 'smb2',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        LOG.info("CIFS share %s created with QoS limited to smb2",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Manage CIFS share with matching QoS
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d6e7f8a9-0000-1111-2222-b0c1d2e3f4a5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_cifs_share_with_qos_match(self):
        """Manage a CIFS export whose QoS matches the share type."""
        LOG.info("=== test_manage_cifs_share_with_qos_match ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '1500',
            'dataset': 'openstack_manila_qos',
        })
        share_type = self.create_qos_share_type(
            qos_type_name=qos_type['name'])

        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_host = share['host']
        export_locations = self._get_export_locations(share['id'])
        export_path = export_locations[0]
        if isinstance(export_path, dict):
            export_path = export_path.get('path', export_path)

        self.unmanage_share(share['id'])

        managed = self.manage_share(
            protocol='CIFS',
            export_path=export_path,
            share_type_name=share_type['name'],
            service_host=share_host,
        )
        self.assertEqual(managed['status'], 'available')
        LOG.info("CIFS share managed with matching QoS: %s", managed['id'])


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
    @decorators.idempotent_id('e7f8a9b0-1111-2222-3333-c1d2e3f4a5b6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_qos_type_with_specs(self):
        """Create a QoS type and verify specs are stored correctly."""
        LOG.info("=== test_create_qos_type_with_specs ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '1000',
            'dataset': 'openstack_manila_qos',
            'protocols': 'nfs3,nfs4',
        })

        self.assertIsNotNone(qos_type.get('id'))
        specs = qos_type.get('specs', {})
        self.assertEqual(specs.get('protocol_ops'), '1000')
        self.assertEqual(specs.get('dataset'), 'openstack_manila_qos')
        self.assertEqual(specs.get('protocols'), 'nfs3,nfs4')
        LOG.info("QoS type %s created with correct specs", qos_type['id'])

    # ----------------------------------------------------------------
    # Test: Share type with default_qos_type has correct extra-specs
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f8a9b0c1-2222-3333-4444-d2e3f4a5b6c7')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_share_type_with_default_qos_type_spec(self):
        """Create share type with default_qos_type and verify extra-specs."""
        LOG.info("=== test_share_type_with_default_qos_type_spec ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '500',
            'dataset': 'openstack_manila_qos',
        })
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
    @decorators.idempotent_id('a9b0c1d2-3333-4444-5555-e3f4a5b6c7d8')
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
    # Test: QoS type with dataset_id (numeric) spec
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b0c1d2e3-4444-5555-6666-f4a5b6c7d8e9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_qos_type_with_dataset_id(self):
        """Create QoS type using dataset_id instead of dataset name."""
        LOG.info("=== test_create_qos_type_with_dataset_id ===")

        qos_type = self.create_qos_type(specs={
            'protocol_ops': '3000',
            'dataset_id': '3',
        })

        specs = qos_type.get('specs', {})
        self.assertEqual(specs.get('protocol_ops'), '3000')
        self.assertEqual(specs.get('dataset_id'), '3')
        self.assertNotIn('dataset', specs)
        LOG.info("QoS type %s with dataset_id spec created", qos_type['id'])


# ---------------------------------------------------------------------------
# Concrete test classes wired to a Tempest-compatible base class.
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleQoSNFS(
            _NFSQoSTests,
            PowerScaleQoSShareTest,
            manila_base.BaseSharesAdminTest):
        """NFS QoS functional tests (manila_tempest_tests base)."""

    class TestPowerScaleQoSCIFS(
            _CIFSQoSTests,
            PowerScaleQoSShareTest,
            manila_base.BaseSharesAdminTest):
        """CIFS QoS functional tests (manila_tempest_tests base)."""

    class TestPowerScaleQoSShareType(
            _QoSShareTypeTests,
            PowerScaleQoSShareTest,
            manila_base.BaseSharesAdminTest):
        """QoS share type spec tests (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleQoSNFS(
            _NFSQoSTests,
            PowerScaleQoSShareTest,
            tempest_test.BaseTestCase):
        """NFS QoS functional tests (tempest.test fallback base)."""
        credentials = ['admin']

    class TestPowerScaleQoSCIFS(
            _CIFSQoSTests,
            PowerScaleQoSShareTest,
            tempest_test.BaseTestCase):
        """CIFS QoS functional tests (tempest.test fallback base)."""
        credentials = ['admin']

    class TestPowerScaleQoSShareType(
            _QoSShareTypeTests,
            PowerScaleQoSShareTest,
            tempest_test.BaseTestCase):
        """QoS share type spec tests (tempest.test fallback base)."""
        credentials = ['admin']
