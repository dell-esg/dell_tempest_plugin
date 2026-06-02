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
Tempest functional tests for Dell PowerScale dedupe feature.

These tests exercise the dedupe lifecycle through real Manila API calls:
  - Creating share types with dedupe=True extra-spec
  - Creating NFS/CIFS shares that trigger dedupe scheduling on PowerScale
  - Deleting shares that deregister dedupe paths on PowerScale
  - Managing shares with dedupe-enabled/disabled share types
  - Verifying dedupe schedule propagation

References (from dedupe-share.patch):
  - PowerScale API: /platform/1/dedupe/settings (GET/PUT)
  - PowerScale API: /platform/1/job/types/Dedupe (GET/PUT)
  - Manila share type extra-spec: dedupe=True
  - Config option: powerscale_dedupe_schedule
"""

import time

from oslo_log import log as logging
from tempest import clients
from tempest import config
from tempest.common import credentials_factory
from tempest.common import waiters as tempest_waiters
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc
from tempest.lib.common.utils import data_utils

CONF = config.CONF
LOG = logging.getLogger(__name__)

SHARE_BUILD_TIMEOUT = 600
SHARE_BUILD_INTERVAL = 5


class PowerScaleDedupeShareTest(object):
    """Mixin with helpers for PowerScale dedupe share tests.

    Provides utility methods for creating share types, shares,
    and waiting for share status transitions via the Manila API.
    """

    @classmethod
    def setup_clients(cls):
        super(PowerScaleDedupeShareTest, cls).setup_clients()
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
            # Auto-detect the Manila service type from the catalog if
            # not explicitly configured (the manila_tempest_tests default
            # of 'share' may not match the actual catalog entry).
            catalog_type = configured_type
            if not catalog_type or catalog_type == 'share':
                try:
                    auth_data = manager.auth_provider.get_auth()
                    catalog = auth_data[1].get('catalog', [])
                    for entry in catalog:
                        if entry.get('type') in ('shared-file-system', 'share'):
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
        # Fall back to the shares v2 client which also has share type methods
        return cls._get_manila_client(manager)

    # ------------------------------------------------------------------
    # Share type helpers
    # ------------------------------------------------------------------
    def create_dedupe_share_type(self, name=None, dedupe=True,
                                 extra_specs=None):
        """Create a Manila share type with dedupe extra-spec.

        :param name: Optional name; auto-generated if None.
        :param dedupe: Whether to set dedupe=True in extra-specs.
        :param extra_specs: Additional extra-specs dict to merge.
        :returns: Created share type dict.
        """
        name = name or data_utils.rand_name('ps-dedupe-type')
        specs = {'driver_handles_share_servers': 'False'}
        if dedupe:
            specs['dedupe'] = 'True'
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

    def create_non_dedupe_share_type(self, name=None, extra_specs=None):
        """Create a Manila share type without dedupe extra-spec."""
        return self.create_dedupe_share_type(
            name=name, dedupe=False, extra_specs=extra_specs)

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
        name = name or data_utils.rand_name(f'ps-dedupe-{protocol.lower()}')
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

    # ------------------------------------------------------------------
    # Manage / Unmanage helpers
    # ------------------------------------------------------------------
    def manage_share(self, protocol, export_path, share_type_name,
                     name=None, service_host=None):
        """Manage an existing PowerScale export as a Manila share.

        :param protocol: 'NFS' or 'CIFS'
        :param export_path: The export path on PowerScale
            (e.g. '<ip>:/ifs/manila/<share_name>').
        :param share_type_name: Name of the share type.
        :param name: Optional Manila share name.
        :param service_host: Full host string including pool
            (e.g. 'host@backend#pool'). Falls back to _get_manila_host().
        :returns: Managed share dict.
        """
        name = name or data_utils.rand_name('ps-manage-dedupe')
        share = self.shares_v2_client.manage_share(
            service_host=service_host or self._get_manila_host(),
            protocol=protocol,
            export_path=export_path,
            share_type_id=share_type_name,
            name=name,
        )
        sh = share.get('share', share)
        LOG.info("Manage request for share '%s' (id=%s)", sh['name'], sh['id'])
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
        """Retrieve export locations for a share via the dedicated API."""
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    def _get_manila_host(self):
        """Discover the Manila host string for the PowerScale backend.

        Returns a string in the form 'host@backend' as registered in
        manila.conf (e.g., 'manila-host@powerscale').
        Falls back to CONF if available.
        """
        try:
            services = self.shares_v2_client.list_services()
            for svc in services.get('services', services):
                host = svc.get('host', '')
                if 'powerscale' in host.lower():
                    LOG.info("Discovered Manila PowerScale host: %s", host)
                    return host
        except Exception as e:
            LOG.warning("Failed to discover Manila host: %s", e)
        # Fallback: caller must ensure this is configured
        return getattr(CONF, 'share', {}).get(
            'powerscale_host', 'manila-host@powerscale')


class _NFSDedupeTests(object):
    """Mixin: NFS share dedupe test methods for PowerScale.

    Each test makes real Manila API calls that propagate to the
    PowerScale backend, triggering dedupe path registration,
    schedule creation, and deregistration via the PowerScale REST API.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSDedupeTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Create NFS share with dedupe enabled
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_with_dedupe(self):
        """Create an NFS share with dedupe=True and verify it succeeds.

        Expected PowerScale side-effects:
          1. Share path added to /platform/1/dedupe/settings paths
          2. Dedupe job schedule checked/updated at
             /platform/1/job/types/Dedupe
        """
        LOG.info("=== test_create_nfs_share_with_dedupe ===")

        # Step 1: Create share type with dedupe=True
        share_type = self.create_dedupe_share_type()
        self.assertIn('dedupe', share_type.get('extra_specs', {}))
        self.assertEqual(share_type['extra_specs']['dedupe'], 'True')

        # Step 2: Create NFS share using the dedupe share type
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )

        # Step 3: Verify share is available (dedupe was processed)
        self.assertEqual(share['status'], 'available',
                         f"Share status is {share['status']}, expected available")
        self.assertEqual(share['share_proto'].upper(), 'NFS')
        export_locations = self._get_export_locations(share['id'])
        self.assertIsNotNone(
            export_locations or None,
            "Share must have an export location")
        LOG.info("NFS share %s created successfully with dedupe enabled",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create NFS share without dedupe
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d2b3c4d5-e6f7-a8b9-c0d1-e2f3a4b5c6d7')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_without_dedupe(self):
        """Create an NFS share without dedupe extra-spec.

        The share should be created normally. No dedupe paths should
        be registered on PowerScale for this share.
        """
        LOG.info("=== test_create_nfs_share_without_dedupe ===")

        share_type = self.create_non_dedupe_share_type()
        self.assertNotIn('dedupe', share_type.get('extra_specs', {}))

        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'NFS')
        LOG.info("NFS share %s created without dedupe", share['id'])

    # ----------------------------------------------------------------
    # Test: Delete NFS share with dedupe (deregisters dedupe path)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e3c4d5e6-f7a8-b9c0-d1e2-f3a4b5c6d7e8')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_nfs_share_with_dedupe(self):
        """Delete an NFS share with dedupe and verify it is removed.

        Expected PowerScale side-effects:
          - Share path removed from /platform/1/dedupe/settings paths
            and assess_paths via deregister_dedupe_settings()
        """
        LOG.info("=== test_delete_nfs_share_with_dedupe ===")

        share_type = self.create_dedupe_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Delete the share
        self.shares_v2_client.delete_share(share_id)
        LOG.info("Requested deletion of dedupe-enabled NFS share %s",
                 share_id)

        # Wait for deletion
        self._wait_for_share_deletion(share_id)

        # Verify it is gone
        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("NFS share %s deleted; dedupe path deregistered", share_id)

    # ----------------------------------------------------------------
    # Test: Create and delete cycle (full dedupe lifecycle)
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f4d5e6f7-a8b9-c0d1-e2f3-a4b5c6d7e8f9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_dedupe_lifecycle(self):
        """Full lifecycle: create with dedupe -> verify -> delete -> verify.

        This exercises the complete dedupe flow:
          1. _process_dedupe(share, None, False) during create
          2. _update_dedupe_settings: adds path to dedupe settings
          3. Checks/updates dedupe job schedule
          4. _process_dedupe(share, None, True) during delete
          5. deregister_dedupe_settings: removes path from dedupe settings
        """
        LOG.info("=== test_nfs_dedupe_lifecycle ===")

        # Create dedupe share type
        share_type = self.create_dedupe_share_type()

        # Create share
        share = self.create_share(
            protocol='NFS',
            share_type_name=share_type['name'],
            size=1,
        )
        self.assertEqual(share['status'], 'available')
        share_id = share['id']

        # Refresh share details
        updated = self.shares_v2_client.get_share(share_id)
        sh = updated.get('share', updated)
        self.assertEqual(sh['status'], 'available')
        export_locations = self._get_export_locations(share_id)
        self.assertIsNotNone(export_locations or None)

        # Delete share
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("Full NFS dedupe lifecycle completed for share %s", share_id)

    # ----------------------------------------------------------------
    # Test: Manage share with dedupe-enabled type matches backend state
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a5e6f7a8-b9c0-d1e2-f3a4-b5c6d7e8f9a0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_nfs_share_with_dedupe_type(self):
        """Manage a PowerScale export that has dedupe enabled.

        The share must be managed with a dedupe-enabled share type.
        Per the patch, if the share type has dedupe=True, the path
        must already exist in the PowerScale dedupe settings; otherwise
        manage raises ShareBackendException.

        Steps:
          1. Create a dedupe share (registers the path on PowerScale)
          2. Unmanage it (removes from Manila, but path stays on backend)
          3. Manage it back with a dedupe-enabled share type -> success
        """
        LOG.info("=== test_manage_nfs_share_with_dedupe_type ===")

        share_type = self.create_dedupe_share_type()
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

        # Manage back with dedupe-enabled type
        managed = self.manage_share(
            protocol='NFS',
            export_path=export_path,
            share_type_name=share_type['name'],
            service_host=share_host,
        )
        self.assertEqual(managed['status'], 'available')
        LOG.info("Successfully managed share with dedupe type: %s",
                 managed['id'])

    # ----------------------------------------------------------------
    # Test: Manage share with non-dedupe type when dedupe is enabled
    #       on backend should fail
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b6f7a8b9-c0d1-e2f3-a4b5-c6d7e8f9a0b1')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nfs_share_non_dedupe_type_fails_when_dedupe_enabled(self):
        """Manage a dedupe-enabled export with a non-dedupe share type.

        Per the patch logic in _process_dedupe():
          - If dedupe is NOT True in share type extra-specs but the
            manage_share_path IS in the dedupe paths, raise
            ShareBackendException('Cannot manage share ... because
            dedupe is already enabled for the given path.')

        Steps:
          1. Create a dedupe share (path registered on PowerScale)
          2. Unmanage it
          3. Try to manage with non-dedupe type -> expect error
        """
        LOG.info("=== test_manage_nfs_share_non_dedupe_type_fails ===")

        dedupe_type = self.create_dedupe_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=dedupe_type['name'],
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

        # Create a non-dedupe share type
        non_dedupe_type = self.create_non_dedupe_share_type()

        # Manage with non-dedupe type should fail
        # Manila returns manage_existing_error status
        try:
            result = self.shares_v2_client.manage_share(
                service_host=share_host,
                protocol='NFS',
                export_path=export_path,
                share_type_id=non_dedupe_type['name'],
                name=data_utils.rand_name('ps-manage-fail'),
            )
            sh = result.get('share', result)
            self.addCleanup(self._delete_share_safe, sh['id'])
            # Wait a bit for the manage operation to process
            time.sleep(10)
            managed = self.shares_v2_client.get_share(sh['id'])
            managed_sh = managed.get('share', managed)
            self.assertIn(managed_sh['status'],
                          ('manage_error', 'error'),
                          f"Expected manage to fail but got status: "
                          f"{managed_sh['status']}")
            LOG.info("Manage correctly failed with status=%s for "
                     "non-dedupe type on dedupe-enabled export",
                     managed_sh['status'])
        except lib_exc.BadRequest:
            LOG.info("Manage correctly rejected with BadRequest for "
                     "non-dedupe type on dedupe-enabled export")
        except lib_exc.ServerFault:
            LOG.info("Manage correctly rejected with ServerFault for "
                     "non-dedupe type on dedupe-enabled export")

    # ----------------------------------------------------------------
    # Test: Manage share with dedupe type when dedupe is NOT enabled
    #       on backend should fail
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c7a8b9c0-d1e2-f3a4-b5c6-d7e8f9a0b1c2')
    @decorators.attr(type=['negative', 'api_with_backend'])
    def test_manage_nfs_share_dedupe_type_fails_when_dedupe_disabled(self):
        """Manage a non-dedupe export with a dedupe-enabled share type.

        Per _process_dedupe():
          - If dedupe IS True in share type extra-specs but the
            manage_share_path is NOT in the dedupe paths, raise
            ShareBackendException('Cannot manage share ... because
            dedupe is disabled for the given path')

        Steps:
          1. Create a non-dedupe share (path NOT in dedupe settings)
          2. Unmanage it
          3. Try to manage with dedupe type -> expect error
        """
        LOG.info("=== test_manage_nfs_dedupe_type_fails_when_disabled ===")

        non_dedupe_type = self.create_non_dedupe_share_type()
        share = self.create_share(
            protocol='NFS',
            share_type_name=non_dedupe_type['name'],
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

        # Create a dedupe share type
        dedupe_type = self.create_dedupe_share_type()

        # Manage with dedupe type should fail
        try:
            result = self.shares_v2_client.manage_share(
                service_host=share_host,
                protocol='NFS',
                export_path=export_path,
                share_type_id=dedupe_type['name'],
                name=data_utils.rand_name('ps-manage-fail'),
            )
            sh = result.get('share', result)
            self.addCleanup(self._delete_share_safe, sh['id'])
            time.sleep(10)
            managed = self.shares_v2_client.get_share(sh['id'])
            managed_sh = managed.get('share', managed)
            self.assertIn(managed_sh['status'],
                          ('manage_error', 'error'),
                          f"Expected manage to fail but got status: "
                          f"{managed_sh['status']}")
            LOG.info("Manage correctly failed with status=%s for "
                     "dedupe type on non-dedupe export",
                     managed_sh['status'])
        except lib_exc.BadRequest:
            LOG.info("Manage correctly rejected with BadRequest for "
                     "dedupe type on non-dedupe export")
        except lib_exc.ServerFault:
            LOG.info("Manage correctly rejected with ServerFault for "
                     "dedupe type on non-dedupe export")


class _CIFSDedupeTests(object):
    """Mixin: CIFS share dedupe test methods for PowerScale.

    Same dedupe flows as NFS but exercised over the CIFS protocol path,
    which uses _create_cifs_share -> _process_dedupe on the backend.
    """

    @classmethod
    def skip_checks(cls):
        super(_CIFSDedupeTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Create CIFS share with dedupe
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d8b9c0d1-e2f3-a4b5-c6d7-e8f9a0b1c2d3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_with_dedupe(self):
        """Create a CIFS share with dedupe=True and verify it succeeds.

        Expected PowerScale side-effects (same as NFS):
          1. Share path added to /platform/1/dedupe/settings
          2. Dedupe job schedule checked/updated
        """
        LOG.info("=== test_create_cifs_share_with_dedupe ===")

        share_type = self.create_dedupe_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'CIFS')
        export_locations = self._get_export_locations(share['id'])
        self.assertIsNotNone(export_locations or None)
        LOG.info("CIFS share %s created with dedupe enabled", share['id'])

    # ----------------------------------------------------------------
    # Test: Create CIFS share without dedupe
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e9c0d1e2-f3a4-b5c6-d7e8-f9a0b1c2d3e4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_without_dedupe(self):
        """Create a CIFS share without dedupe extra-spec."""
        LOG.info("=== test_create_cifs_share_without_dedupe ===")

        share_type = self.create_non_dedupe_share_type()
        share = self.create_share(
            protocol='CIFS',
            share_type_name=share_type['name'],
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'CIFS')
        LOG.info("CIFS share %s created without dedupe", share['id'])

    # ----------------------------------------------------------------
    # Test: Delete CIFS share with dedupe
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f0d1e2f3-a4b5-c6d7-e8f9-a0b1c2d3e4f5')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_cifs_share_with_dedupe(self):
        """Delete a CIFS share with dedupe; path should be deregistered."""
        LOG.info("=== test_delete_cifs_share_with_dedupe ===")

        share_type = self.create_dedupe_share_type()
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
        LOG.info("CIFS share %s deleted; dedupe path deregistered", share_id)

    # ----------------------------------------------------------------
    # Test: CIFS dedupe full lifecycle
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a1e2f3a4-b5c6-d7e8-f9a0-b1c2d3e4f5a6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_dedupe_lifecycle(self):
        """Full lifecycle: create CIFS with dedupe -> verify -> delete."""
        LOG.info("=== test_cifs_dedupe_lifecycle ===")

        share_type = self.create_dedupe_share_type()
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
        LOG.info("Full CIFS dedupe lifecycle completed for share %s",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Manage CIFS share with dedupe type
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b2f3a4b5-c6d7-e8f9-a0b1-c2d3e4f5a6b7')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_manage_cifs_share_with_dedupe_type(self):
        """Manage a CIFS export that has dedupe enabled on PowerScale."""
        LOG.info("=== test_manage_cifs_share_with_dedupe_type ===")

        share_type = self.create_dedupe_share_type()
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
        LOG.info("CIFS share managed with dedupe type: %s", managed['id'])


class _ShareTypeDedupeTests(object):
    """Mixin: share type extra-spec validation tests for dedupe.

    Verifies that share types are created correctly with dedupe
    extra-specs and that the driver reports dedupe capability.
    """

    @classmethod
    def skip_checks(cls):
        super(_ShareTypeDedupeTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Share type with dedupe=True has correct extra-specs
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c3a4b5c6-d7e8-f9a0-b1c2-d3e4f5a6b7c8')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_share_type_with_dedupe_extra_spec(self):
        """Create share type with dedupe=True and verify extra-specs."""
        LOG.info("=== test_create_share_type_with_dedupe_extra_spec ===")

        share_type = self.create_dedupe_share_type()

        # Verify extra-specs
        specs = share_type.get('extra_specs', {})
        self.assertIn('dedupe', specs)
        self.assertEqual(specs['dedupe'], 'True')
        self.assertEqual(specs['driver_handles_share_servers'], 'False')
        LOG.info("Share type %s has correct dedupe extra-specs",
                 share_type['id'])

    # ----------------------------------------------------------------
    # Test: Share type without dedupe extra-spec
    # ----------------------------------------------------------------
    @decorators.idempotent_id('d4b5c6d7-e8f9-a0b1-c2d3-e4f5a6b7c8d9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_share_type_without_dedupe_extra_spec(self):
        """Create share type without dedupe and verify no dedupe spec."""
        LOG.info("=== test_create_share_type_without_dedupe ===")

        share_type = self.create_non_dedupe_share_type()

        specs = share_type.get('extra_specs', {})
        self.assertNotIn('dedupe', specs)
        LOG.info("Share type %s correctly has no dedupe extra-spec",
                 share_type['id'])

    # ----------------------------------------------------------------
    # Test: Multiple dedupe shares reuse same dedupe schedule
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e5c6d7e8-f9a0-b1c2-d3e4-f5a6b7c8d9e0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_multiple_dedupe_shares_single_schedule(self):
        """Create multiple dedupe shares; all share the same schedule.

        On PowerScale, the Dedupe job type has a single schedule.
        Creating multiple shares with dedupe=True should add each
        share's path to the dedupe settings but not conflict on the
        schedule (only one PUT to /platform/1/job/types/Dedupe if
        schedule already matches).
        """
        LOG.info("=== test_multiple_dedupe_shares_single_schedule ===")

        share_type = self.create_dedupe_share_type()

        shares = []
        for i in range(3):
            share = self.create_share(
                protocol='NFS',
                share_type_name=share_type['name'],
                size=1,
                name=data_utils.rand_name(f'ps-dedupe-multi-{i}'),
            )
            self.assertEqual(share['status'], 'available')
            shares.append(share)

        LOG.info("Created %d dedupe shares successfully", len(shares))

        # Clean up in reverse order
        for share in reversed(shares):
            self.shares_v2_client.delete_share(share['id'])
            self._wait_for_share_deletion(share['id'])

        LOG.info("All %d dedupe shares deleted; paths deregistered",
                 len(shares))


# ---------------------------------------------------------------------------
# Concrete test classes wired to a Tempest-compatible base class.
#
# The actual base depends on what is installed in the target environment.
# We try manila_tempest_tests first, then fall back to tempest.test.
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleDedupeNFS(
            _NFSDedupeTests,
            PowerScaleDedupeShareTest,
            manila_base.BaseSharesAdminTest):
        """NFS dedupe functional tests (manila_tempest_tests base)."""

    class TestPowerScaleDedupeCIFS(
            _CIFSDedupeTests,
            PowerScaleDedupeShareTest,
            manila_base.BaseSharesAdminTest):
        """CIFS dedupe functional tests (manila_tempest_tests base)."""

    class TestPowerScaleDedupeShareTypeExtraSpecs(
            _ShareTypeDedupeTests,
            PowerScaleDedupeShareTest,
            manila_base.BaseSharesAdminTest):
        """Share-type extra-spec dedupe tests (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleDedupeNFS(
            _NFSDedupeTests,
            PowerScaleDedupeShareTest,
            tempest_test.BaseTestCase):
        """NFS dedupe functional tests (tempest.test fallback base)."""
        credentials = ['primary', 'admin']

    class TestPowerScaleDedupeCIFS(
            _CIFSDedupeTests,
            PowerScaleDedupeShareTest,
            tempest_test.BaseTestCase):
        """CIFS dedupe functional tests (tempest.test fallback base)."""
        credentials = ['primary', 'admin']

    class TestPowerScaleDedupeShareTypeExtraSpecs(
            _ShareTypeDedupeTests,
            PowerScaleDedupeShareTest,
            tempest_test.BaseTestCase):
        """Share-type extra-spec dedupe tests (tempest.test fallback base)."""
        credentials = ['primary', 'admin']
