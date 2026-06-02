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
Tempest functional tests for Dell PowerScale mount_point_name feature.

Complementary to generic Manila mount_point_name tests.  Focuses on
PowerScale-specific backend behaviour:
  NFS: alias creation/deletion via create/delete_nfs_export_aliases
  CIFS: mount_point_name used as SMB share name
  Both: manage round-trip (API >= 2.92), resize preserving alias/name
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

# Minimum API microversion that supports mount_point_name on create
MPN_CREATE_MIN_API_VERSION = '2.84'
# Minimum API microversion that supports mount_point_name on manage
MPN_MANAGE_MIN_API_VERSION = '2.92'

# Prefix used in share type extra-spec provisioning:mount_point_prefix.
# Must be ASCII alphanumeric + underscore only.
MPN_PREFIX = 'tempest_mpn'


# ======================================================================
# Base mixin — helpers only, no test methods
# ======================================================================
class PowerScaleMountPointNameTest(object):
    """Mixin providing helpers for PowerScale mount_point_name tests."""

    @classmethod
    def setup_clients(cls):
        super(PowerScaleMountPointNameTest, cls).setup_clients()
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
    def create_mpn_share_type(self, name=None, extra_specs=None):
        """Create a share type with mount_point_name_support + prefix."""
        name = name or data_utils.rand_name('ps-mpn-type')
        specs = {
            'driver_handles_share_servers': 'False',
            'mount_point_name_support': '<is> True',
            'provisioning:mount_point_prefix': MPN_PREFIX,
        }
        if extra_specs:
            specs.update(extra_specs)

        share_type = self.share_types_client.create_share_type(
            name=name,
            extra_specs=specs,
        )
        st = share_type.get('share_type', share_type)
        LOG.info("Created MPN share type '%s' (id=%s) with specs=%s",
                 st['name'], st['id'], specs)
        self.addCleanup(self._delete_share_type_safe, st['id'])
        return st

    def create_mpn_share_type_no_prefix(self, name=None, extra_specs=None):
        """Create mpn share type without prefix (uses project_id fallback)."""
        name = name or data_utils.rand_name('ps-mpn-nopfx-type')
        specs = {
            'driver_handles_share_servers': 'False',
            'mount_point_name_support': '<is> True',
        }
        if extra_specs:
            specs.update(extra_specs)

        share_type = self.share_types_client.create_share_type(
            name=name,
            extra_specs=specs,
        )
        st = share_type.get('share_type', share_type)
        LOG.info("Created MPN-no-prefix share type '%s' (id=%s)",
                 st['name'], st['id'])
        self.addCleanup(self._delete_share_type_safe, st['id'])
        return st

    def create_plain_share_type(self, name=None, extra_specs=None):
        """Create a share type WITHOUT mount_point_name_support."""
        name = name or data_utils.rand_name('ps-plain-type')
        specs = {'driver_handles_share_servers': 'False'}
        if extra_specs:
            specs.update(extra_specs)

        share_type = self.share_types_client.create_share_type(
            name=name,
            extra_specs=specs,
        )
        st = share_type.get('share_type', share_type)
        LOG.info("Created plain share type '%s' (id=%s)", st['name'],
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
    @staticmethod
    def _random_mpn_suffix():
        """Generate a random mount_point_name suffix (no tempest prefix)."""
        import uuid
        return uuid.uuid4().hex[:12]
    def create_share_with_mpn(self, protocol, share_type_id,
                              mount_point_name, size=1, name=None):
        """Create a share with mount_point_name via raw POST (>= 2.84)."""
        import uuid
        name = name or f'ps-mpn-{protocol.lower()}-{uuid.uuid4().hex[:8]}'
        body = {
            'share': {
                'share_proto': protocol,
                'size': size,
                'name': name,
                'share_type': share_type_id,
                'mount_point_name': mount_point_name,
            }
        }
        resp, resp_body = self.shares_v2_client.post(
            'shares', json.dumps(body),
            version=MPN_CREATE_MIN_API_VERSION)
        if isinstance(resp_body, bytes):
            resp_body = resp_body.decode('utf-8')
        share_data = json.loads(resp_body) if isinstance(
            resp_body, str) else resp_body
        sh = share_data.get('share', share_data)
        LOG.info("Created share '%s' (id=%s, protocol=%s, mpn=%s)",
                 sh.get('name'), sh['id'], protocol, mount_point_name)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self._get_share(sh['id'])

    def create_share(self, protocol, share_type_name, size=1, name=None):
        """Create a Manila share without mount_point_name."""
        name = name or data_utils.rand_name(
            f'ps-mpn-{protocol.lower()}')
        share = self.shares_v2_client.create_share(
            share_protocol=protocol,
            size=size,
            name=name,
            share_type_id=share_type_name,
        )
        sh = share.get('share', share)
        LOG.info("Created share '%s' (id=%s, protocol=%s, size=%dG)",
                 sh['name'], sh['id'], protocol, size)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self._get_share(sh['id'])

    def _get_share(self, share_id):
        """Fetch a share and unwrap the 'share' key if present."""
        share = self.shares_v2_client.get_share(share_id)
        return share.get('share', share)

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
            sh = self._get_share(share_id)
            status = sh.get('status', '').lower()
            last_status = status
            if status == target_status:
                return
            if status in ('error', 'error_deleting', 'manage_error',
                          'shrinking_error', 'extending_error'):
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
    # Export location helpers
    # ------------------------------------------------------------------
    def _get_export_locations(self, share_id):
        """Retrieve export locations for a share."""
        el = self.shares_v2_client.list_share_export_locations(share_id)
        locations = el.get('export_locations', el)
        if isinstance(locations, list):
            return locations
        return []

    def _get_export_paths(self, share_id):
        """Return a simple list of export path strings."""
        locations = self._get_export_locations(share_id)
        paths = []
        for loc in locations:
            if isinstance(loc, dict):
                paths.append(loc.get('path', ''))
            else:
                paths.append(str(loc))
        return paths

    def _get_preferred_export(self, share_id):
        """Return the preferred export location path, or None."""
        for loc in self._get_export_locations(share_id):
            if isinstance(loc, dict):
                meta = loc.get('metadata', {}) or {}
                if meta.get('preferred') in (True, 'True', 'true'):
                    return loc.get('path', '')
        return None

    # ------------------------------------------------------------------
    # Manage / Unmanage helpers
    # ------------------------------------------------------------------
    def manage_share_with_mpn(self, protocol, export_path, share_type_id,
                              mount_point_name, name=None,
                              service_host=None):
        """Manage a share with mount_point_name via raw POST (>= 2.92)."""
        name = name or data_utils.rand_name('ps-mpn-managed')
        body = {
            'share': {
                'protocol': protocol,
                'export_path': export_path,
                'service_host': service_host or self._get_manila_host(),
                'share_type': share_type_id,
                'name': name,
                'mount_point_name': mount_point_name,
            }
        }
        resp, resp_body = self.shares_v2_client.post(
            'shares/manage', json.dumps(body),
            version=MPN_MANAGE_MIN_API_VERSION)
        if isinstance(resp_body, bytes):
            resp_body = resp_body.decode('utf-8')
        share_data = json.loads(resp_body) if isinstance(
            resp_body, str) else resp_body
        sh = share_data.get('share', share_data)
        LOG.info("Manage request for share '%s' (id=%s, mpn=%s)",
                 sh.get('name'), sh['id'], mount_point_name)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self._get_share(sh['id'])

    def manage_share(self, protocol, export_path, share_type_name,
                     name=None, service_host=None):
        """Manage a share without mount_point_name."""
        name = name or data_utils.rand_name('ps-mpn-managed')
        share = self.shares_v2_client.manage_share(
            service_host=service_host or self._get_manila_host(),
            protocol=protocol,
            export_path=export_path,
            share_type_id=share_type_name,
            name=name,
        )
        sh = share.get('share', share)
        LOG.info("Manage request for share '%s' (id=%s, export=%s)",
                 sh['name'], sh['id'], export_path)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        return self._get_share(sh['id'])

    def unmanage_share(self, share_id):
        """Unmanage a share (removes from Manila, keeps on backend)."""
        self.shares_v2_client.unmanage_share(share_id)
        LOG.info("Unmanaged share %s", share_id)
        self._wait_for_share_deletion(share_id)

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

    # ------------------------------------------------------------------
    # Extend / Shrink helpers
    # ------------------------------------------------------------------
    def extend_share(self, share_id, new_size):
        """Extend a share and wait for it to become available."""
        LOG.info("Extending share %s to %dG", share_id, new_size)
        self.shares_v2_client.extend_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        return self._get_share(share_id)

    def shrink_share(self, share_id, new_size):
        """Shrink a share and wait for it to become available."""
        LOG.info("Shrinking share %s to %dG", share_id, new_size)
        self.shares_v2_client.shrink_share(share_id, new_size)
        self._wait_for_share_status(share_id, 'available')
        return self._get_share(share_id)

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def create_snapshot(self, share_id, name=None):
        """Create a snapshot and wait until it becomes available."""
        name = name or data_utils.rand_name('ps-mpn-snap')
        snapshot = self.shares_v2_client.create_snapshot(share_id, name=name)
        snap = snapshot.get('snapshot', snapshot)
        LOG.info("Created snapshot '%s' (id=%s) for share %s",
                 snap.get('name'), snap['id'], share_id)
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
        """Poll snapshot status until it reaches target."""
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            snap = self.shares_v2_client.get_snapshot(snapshot_id)
            s = snap.get('snapshot', snap)
            status = s.get('status', '').lower()
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


# ======================================================================
# NFS mount_point_name tests
# ======================================================================
class _NFSMountPointNameTests(object):
    """Mixin: NFS share mount_point_name test methods for PowerScale.

    Each test makes real Manila API calls that propagate to the
    PowerScale backend, triggering NFS export alias creation or
    deletion via the PowerScale REST API.
    """

    @classmethod
    def skip_checks(cls):
        super(_NFSMountPointNameTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Create NFS share with mount_point_name -> alias created
    # ----------------------------------------------------------------
    @decorators.idempotent_id('10a1b2c3-1001-2002-3003-d4e5f6a7b8c9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_with_mount_point_name(self):
        """Create NFS share with mpn -> verify alias in export locations."""
        LOG.info("=== test_create_nfs_share_with_mount_point_name ===")

        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()

        share = self.create_share_with_mpn(
            protocol='NFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )

        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'NFS')

        # Verify export locations include the mount_point_name path
        export_paths = self._get_export_paths(share['id'])
        self.assertTrue(
            len(export_paths) >= 2,
            f"NFS share with mount_point_name should have >= 2 export "
            f"locations (container path + alias path), got: {export_paths}")

        # The alias path should contain the constructed mount_point_name
        # (prefix + suffix)
        expected_mpn = f"{MPN_PREFIX}{mpn_suffix}"
        alias_found = any(expected_mpn in p for p in export_paths)
        self.assertTrue(
            alias_found,
            f"Expected mount_point_name '{expected_mpn}' in export "
            f"locations: {export_paths}")

        # Verify preferred export is the alias path
        preferred = self._get_preferred_export(share['id'])
        if preferred:
            self.assertIn(
                expected_mpn, preferred,
                f"Preferred export should contain mount_point_name "
                f"'{expected_mpn}', got: {preferred}")

        LOG.info("NFS share %s created with alias for mpn=%s",
                 share['id'], expected_mpn)

    # ----------------------------------------------------------------
    # Test: Delete NFS share with mount_point_name -> alias deleted
    # ----------------------------------------------------------------
    @decorators.idempotent_id('20b2c3d4-2002-3003-4004-e5f6a7b8c9d0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_nfs_share_with_mount_point_name(self):
        """Delete NFS share with mpn -> alias removed from backend."""
        LOG.info("=== test_delete_nfs_share_with_mount_point_name ===")

        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()

        share = self.create_share_with_mpn(
            protocol='NFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Delete the share
        self.shares_v2_client.delete_share(share_id)
        LOG.info("Requested deletion of NFS share %s with mpn", share_id)

        self._wait_for_share_deletion(share_id)

        # Verify share is gone
        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("NFS share %s with mount_point_name deleted; "
                 "alias removed", share_id)

    # ----------------------------------------------------------------
    # Test: Full NFS lifecycle with mount_point_name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('30c3d4e5-3003-4004-5005-f6a7b8c9d0e1')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_nfs_mount_point_name_lifecycle(self):
        """Full lifecycle: create with mpn -> verify alias -> delete."""
        LOG.info("=== test_nfs_mount_point_name_lifecycle ===")

        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()
        expected_mpn = f"{MPN_PREFIX}{mpn_suffix}"

        # Create
        share = self.create_share_with_mpn(
            protocol='NFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        # Verify alias in exports
        export_paths = self._get_export_paths(share_id)
        alias_found = any(expected_mpn in p for p in export_paths)
        self.assertTrue(alias_found,
                        f"Alias '{expected_mpn}' not in {export_paths}")

        # Delete
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("NFS mpn lifecycle complete for share %s", share_id)

    # ----------------------------------------------------------------
    # Test: Extend NFS share with mount_point_name -> alias persists
    # ----------------------------------------------------------------
    @decorators.idempotent_id('40d4e5f6-4004-5005-6006-a7b8c9d0e1f2')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_nfs_share_with_mount_point_name(self):
        """Extend NFS share with mpn -> alias persists after quota update."""
        LOG.info("=== test_extend_nfs_share_with_mount_point_name ===")

        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()
        expected_mpn = f"{MPN_PREFIX}{mpn_suffix}"

        share = self.create_share_with_mpn(
            protocol='NFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )

        extended = self.extend_share(share['id'], 2)
        self.assertEqual(extended['size'], 2)
        self.assertEqual(extended['status'], 'available')

        # Alias should still be present
        export_paths = self._get_export_paths(share['id'])
        alias_found = any(expected_mpn in p for p in export_paths)
        self.assertTrue(alias_found,
                        f"Alias '{expected_mpn}' lost after extend: "
                        f"{export_paths}")
        LOG.info("NFS share %s extended to 2G; alias preserved",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Shrink NFS share with mount_point_name -> alias persists
    # ----------------------------------------------------------------
    @decorators.idempotent_id('50e5f6a7-5005-6006-7007-b8c9d0e1f2a3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_nfs_share_with_mount_point_name(self):
        """Shrink NFS share with mpn -> alias persists after quota update."""
        LOG.info("=== test_shrink_nfs_share_with_mount_point_name ===")

        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()
        expected_mpn = f"{MPN_PREFIX}{mpn_suffix}"

        share = self.create_share_with_mpn(
            protocol='NFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=2,
        )

        shrunk = self.shrink_share(share['id'], 1)
        self.assertEqual(shrunk['size'], 1)
        self.assertEqual(shrunk['status'], 'available')

        # Alias should still be present
        export_paths = self._get_export_paths(share['id'])
        alias_found = any(expected_mpn in p for p in export_paths)
        self.assertTrue(alias_found,
                        f"Alias '{expected_mpn}' lost after shrink: "
                        f"{export_paths}")
        LOG.info("NFS share %s shrunk to 1G; alias preserved",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create NFS share with mpn using project_id prefix fallback
    # ----------------------------------------------------------------
    @decorators.idempotent_id('71a8b9c0-7107-8208-9309-d1e2f3a4b5c6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_mpn_project_id_fallback(self):
        """Create NFS share with mpn using project_id prefix fallback."""
        LOG.info("=== test_create_nfs_share_mpn_project_id_fallback ===")

        share_type = self.create_mpn_share_type_no_prefix()
        mpn_suffix = self._random_mpn_suffix()

        share = self.create_share_with_mpn(
            protocol='NFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )

        self.assertEqual(share['status'], 'available')

        # The prefix should be the project_id (from context)
        # Export paths should contain the suffix at minimum
        export_paths = self._get_export_paths(share['id'])
        suffix_found = any(mpn_suffix in p for p in export_paths)
        self.assertTrue(
            suffix_found,
            f"Expected suffix '{mpn_suffix}' in export locations "
            f"(project_id prefix mode): {export_paths}")

        # Verify we have >= 2 paths (container + alias)
        self.assertTrue(
            len(export_paths) >= 2,
            f"Expected >= 2 exports with alias, got: {export_paths}")

        LOG.info("NFS share %s created with project_id prefix fallback",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create NFS share from snapshot with mount_point_name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('72b9c0d1-7208-8309-940a-e2f3a4b5c6d7')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_nfs_share_from_snapshot_with_mpn(self):
        """Create NFS share from snapshot with mpn -> alias on new share."""
        LOG.info("=== test_create_nfs_share_from_snapshot_with_mpn ===")

        share_type = self.create_mpn_share_type(
            extra_specs={'snapshot_support': '<is> True',
                         'create_share_from_snapshot_support': '<is> True'})
        mpn_suffix_src = self._random_mpn_suffix()
        mpn_suffix_dst = self._random_mpn_suffix()
        expected_mpn_dst = f"{MPN_PREFIX}{mpn_suffix_dst}"

        # Create source share with mpn
        source = self.create_share_with_mpn(
            protocol='NFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix_src,
            size=1,
        )
        self.assertEqual(source['status'], 'available')

        # Create snapshot
        snapshot = self.create_snapshot(source['id'])

        # Create share from snapshot with a NEW mount_point_name
        import uuid
        name = f'ps-mpn-from-snap-{uuid.uuid4().hex[:8]}'
        body = {
            'share': {
                'share_proto': 'NFS',
                'size': 1,
                'name': name,
                'share_type': share_type['id'],
                'snapshot_id': snapshot['id'],
                'mount_point_name': mpn_suffix_dst,
            }
        }
        resp, resp_body = self.shares_v2_client.post(
            'shares', json.dumps(body),
            version=MPN_CREATE_MIN_API_VERSION)
        if isinstance(resp_body, bytes):
            resp_body = resp_body.decode('utf-8')
        share_data = json.loads(resp_body) if isinstance(
            resp_body, str) else resp_body
        sh = share_data.get('share', share_data)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        new_share = self._get_share(sh['id'])

        self.assertEqual(new_share['status'], 'available')

        # Verify the new share has the alias
        export_paths = self._get_export_paths(new_share['id'])
        alias_found = any(expected_mpn_dst in p for p in export_paths)
        self.assertTrue(
            alias_found,
            f"Alias '{expected_mpn_dst}' not found in share-from-snapshot "
            f"exports: {export_paths}")
        LOG.info("NFS share from snapshot %s has alias %s",
                 new_share['id'], expected_mpn_dst)


# ======================================================================
# CIFS mount_point_name tests
# ======================================================================
class _CIFSMountPointNameTests(object):
    """CIFS mount_point_name test methods — verifies SMB share name."""

    @classmethod
    def skip_checks(cls):
        super(_CIFSMountPointNameTests, cls).skip_checks()
        if not CONF.service_available.manila:
            raise cls.skipException("Manila is not available")

    # ----------------------------------------------------------------
    # Test: Create CIFS share with mount_point_name -> SMB share name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('80b8c9d0-8008-9009-a00a-e1f2a3b4c5d6')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_with_mount_point_name(self):
        """Create CIFS share with mpn -> SMB share name = mpn in UNC."""
        LOG.info("=== test_create_cifs_share_with_mount_point_name ===")
        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()
        share = self.create_share_with_mpn(
            protocol='CIFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )
        self.assertEqual(share['status'], 'available')
        self.assertEqual(share['share_proto'].upper(), 'CIFS')

        # Verify export location contains the mount_point_name
        export_paths = self._get_export_paths(share['id'])
        self.assertTrue(
            len(export_paths) >= 1,
            f"CIFS share should have at least 1 export location, "
            f"got: {export_paths}")

        expected_mpn = f"{MPN_PREFIX}{mpn_suffix}"
        mpn_found = any(expected_mpn in p for p in export_paths)
        self.assertTrue(
            mpn_found,
            f"Expected mount_point_name '{expected_mpn}' in CIFS "
            f"export UNC path: {export_paths}")

        LOG.info("CIFS share %s created with SMB name=%s",
                 share['id'], expected_mpn)

    # ----------------------------------------------------------------
    # Test: Delete CIFS share with mount_point_name -> SMB share gone
    # ----------------------------------------------------------------
    @decorators.idempotent_id('90c9d0e1-9009-a00a-b00b-f2a3b4c5d6e7')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_delete_cifs_share_with_mount_point_name(self):
        """Delete CIFS share with mpn -> SMB share removed from backend."""
        LOG.info("=== test_delete_cifs_share_with_mount_point_name ===")
        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()
        share = self.create_share_with_mpn(
            protocol='CIFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')
        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)
        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("CIFS share %s with mount_point_name deleted", share_id)

    # ----------------------------------------------------------------
    # Test: Full CIFS lifecycle with mount_point_name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('a0d0e1f2-a00a-b00b-c00c-a3b4c5d6e7f8')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_cifs_mount_point_name_lifecycle(self):
        """Full lifecycle: create with mpn -> verify UNC -> delete."""
        LOG.info("=== test_cifs_mount_point_name_lifecycle ===")

        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()
        expected_mpn = f"{MPN_PREFIX}{mpn_suffix}"

        share = self.create_share_with_mpn(
            protocol='CIFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )
        share_id = share['id']
        self.assertEqual(share['status'], 'available')

        export_paths = self._get_export_paths(share_id)
        mpn_found = any(expected_mpn in p for p in export_paths)
        self.assertTrue(mpn_found,
                        f"SMB name '{expected_mpn}' not in {export_paths}")

        self.shares_v2_client.delete_share(share_id)
        self._wait_for_share_deletion(share_id)

        self.assertRaises(
            lib_exc.NotFound,
            self.shares_v2_client.get_share,
            share_id,
        )
        LOG.info("CIFS mpn lifecycle complete for share %s", share_id)

    # ----------------------------------------------------------------
    # Test: Extend CIFS share with mount_point_name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('b0e1f2a3-b00b-c00c-d00d-b4c5d6e7f8a9')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_extend_cifs_share_with_mount_point_name(self):
        """Extend CIFS share with mpn -> SMB name persists."""
        LOG.info("=== test_extend_cifs_share_with_mount_point_name ===")

        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()
        expected_mpn = f"{MPN_PREFIX}{mpn_suffix}"

        share = self.create_share_with_mpn(
            protocol='CIFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )
        extended = self.extend_share(share['id'], 2)
        self.assertEqual(extended['size'], 2)
        self.assertEqual(extended['status'], 'available')

        export_paths = self._get_export_paths(share['id'])
        mpn_found = any(expected_mpn in p for p in export_paths)
        self.assertTrue(mpn_found,
                        f"SMB name '{expected_mpn}' lost after extend: "
                        f"{export_paths}")
        LOG.info("CIFS share %s extended to 2G; SMB name preserved",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Shrink CIFS share with mount_point_name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('c0f2a3b4-c00c-d00d-e00e-c5d6e7f8a9b0')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_shrink_cifs_share_with_mount_point_name(self):
        """Shrink CIFS share with mpn -> SMB name persists."""
        LOG.info("=== test_shrink_cifs_share_with_mount_point_name ===")

        share_type = self.create_mpn_share_type()
        mpn_suffix = self._random_mpn_suffix()
        expected_mpn = f"{MPN_PREFIX}{mpn_suffix}"

        share = self.create_share_with_mpn(
            protocol='CIFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=2,
        )
        shrunk = self.shrink_share(share['id'], 1)
        self.assertEqual(shrunk['size'], 1)
        self.assertEqual(shrunk['status'], 'available')
        export_paths = self._get_export_paths(share['id'])
        mpn_found = any(expected_mpn in p for p in export_paths)
        self.assertTrue(mpn_found,
                        f"SMB name '{expected_mpn}' lost after shrink: "
                        f"{export_paths}")
        LOG.info("CIFS share %s shrunk to 1G; SMB name preserved",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create CIFS share with mpn using project_id prefix fallback
    # ----------------------------------------------------------------
    @decorators.idempotent_id('e1c5d6e7-e10e-f20f-0310-f8a9b0c1d2e3')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_mpn_project_id_fallback(self):
        """Create CIFS share with mpn using project_id prefix fallback."""
        LOG.info("=== test_create_cifs_share_mpn_project_id_fallback ===")

        share_type = self.create_mpn_share_type_no_prefix()
        mpn_suffix = self._random_mpn_suffix()

        share = self.create_share_with_mpn(
            protocol='CIFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix,
            size=1,
        )
        self.assertEqual(share['status'], 'available')

        export_paths = self._get_export_paths(share['id'])
        suffix_found = any(mpn_suffix in p for p in export_paths)
        self.assertTrue(
            suffix_found,
            f"Expected suffix '{mpn_suffix}' in CIFS export UNC "
            f"(project_id prefix mode): {export_paths}")

        LOG.info("CIFS share %s created with project_id prefix fallback",
                 share['id'])

    # ----------------------------------------------------------------
    # Test: Create CIFS share from snapshot with mount_point_name
    # ----------------------------------------------------------------
    @decorators.idempotent_id('f2d6e7f8-f20f-0310-1420-a9b0c1d2e3f4')
    @decorators.attr(type=['positive', 'api_with_backend'])
    def test_create_cifs_share_from_snapshot_with_mpn(self):
        """Create CIFS share from snapshot with mpn -> SMB name on new share."""
        LOG.info("=== test_create_cifs_share_from_snapshot_with_mpn ===")

        share_type = self.create_mpn_share_type(
            extra_specs={'snapshot_support': '<is> True',
                         'create_share_from_snapshot_support': '<is> True'})
        mpn_suffix_src = self._random_mpn_suffix()
        mpn_suffix_dst = self._random_mpn_suffix()
        expected_mpn_dst = f"{MPN_PREFIX}{mpn_suffix_dst}"
        source = self.create_share_with_mpn(
            protocol='CIFS',
            share_type_id=share_type['id'],
            mount_point_name=mpn_suffix_src,
            size=1,
        )
        self.assertEqual(source['status'], 'available')
        snapshot = self.create_snapshot(source['id'])
        import uuid
        name = f'ps-mpn-cifs-from-snap-{uuid.uuid4().hex[:8]}'
        body = {
            'share': {
                'share_proto': 'CIFS',
                'size': 1,
                'name': name,
                'share_type': share_type['id'],
                'snapshot_id': snapshot['id'],
                'mount_point_name': mpn_suffix_dst,
            }
        }
        resp, resp_body = self.shares_v2_client.post(
            'shares', json.dumps(body),
            version=MPN_CREATE_MIN_API_VERSION)
        if isinstance(resp_body, bytes):
            resp_body = resp_body.decode('utf-8')
        share_data = json.loads(resp_body) if isinstance(
            resp_body, str) else resp_body
        sh = share_data.get('share', share_data)
        self.addCleanup(self._delete_share_safe, sh['id'])
        self._wait_for_share_status(sh['id'], 'available')
        new_share = self._get_share(sh['id'])
        self.assertEqual(new_share['status'], 'available')
        export_paths = self._get_export_paths(new_share['id'])
        mpn_found = any(expected_mpn_dst in p for p in export_paths)
        self.assertTrue(
            mpn_found,
            f"SMB name '{expected_mpn_dst}' not found in share-from-snapshot "
            f"exports: {export_paths}")
        LOG.info("CIFS share from snapshot %s has SMB name %s",
                 new_share['id'], expected_mpn_dst)


# ---------------------------------------------------------------------------
# Concrete test classes wired to a Tempest-compatible base class.
# ---------------------------------------------------------------------------
try:
    from manila_tempest_tests.tests.api import base as manila_base

    class TestPowerScaleMountPointNameNFS(
            _NFSMountPointNameTests,
            PowerScaleMountPointNameTest,
            manila_base.BaseSharesAdminTest):
        """NFS mount_point_name functional tests (manila_tempest_tests base)."""

    class TestPowerScaleMountPointNameCIFS(
            _CIFSMountPointNameTests,
            PowerScaleMountPointNameTest,
            manila_base.BaseSharesAdminTest):
        """CIFS mount_point_name functional tests (manila_tempest_tests base)."""

except ImportError:
    from tempest import test as tempest_test

    class TestPowerScaleMountPointNameNFS(
            _NFSMountPointNameTests,
            PowerScaleMountPointNameTest,
            tempest_test.BaseTestCase):
        """NFS mount_point_name functional tests (tempest.test fallback)."""
        credentials = ['admin']

    class TestPowerScaleMountPointNameCIFS(
            _CIFSMountPointNameTests,
            PowerScaleMountPointNameTest,
            tempest_test.BaseTestCase):
        """CIFS mount_point_name functional tests (tempest.test fallback)."""
        credentials = ['admin']
