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
Tempest functional tests for the Dell PowerStore Cinder QoS feature.

These tests drive real Cinder API calls (QoS specs, volume types, volumes,
``os-initialize_connection`` / ``os-terminate_connection``) that make the
PowerStore driver issue real PowerStore REST calls, and then verify the
resulting array state directly through the PowerStore REST API.

How the driver applies QoS (adapter.py)
---------------------------------------
QoS is applied at *attach* time, not at volume creation time:

  * initialize_connection -> ``_connect_volume`` -> ``_attach_volume_to_host``
    -> ``_create_or_update_volume_qos_policy(ATTACH)``
        - only when the QoS spec consumer is ``back-end`` or ``both``
        - only when the array runs PowerStore 4.0 or later
        - ``_get_or_create_qos_policy``:
            policy ``qos-policy-<qos_id>`` missing -> create io_limit_rule
            ``io-rule-<qos_id>`` + create policy, return policy id
            policy exists -> PATCH the io_limit_rule with current specs
        - PATCH /volume/<id> qos_performance_policy_id=<policy id>
  * terminate_connection -> ``_disconnect_volume`` -> ``_detach_volume_from_hosts``
    -> ``_create_or_update_volume_qos_policy(DETACH)``
        - PATCH /volume/<id> qos_performance_policy_id=null

The driver never deletes the io_limit_rule/policy; the tests below remove
the objects they created (named after their own QoS spec id) on cleanup and
never touch objects that were not created by the test.

PowerStore REST endpoints exercised (via the driver)
----------------------------------------------------
  GET   /policy?name=eq.<n>&type=eq.QoS
  POST  /io_limit_rule
  POST  /policy
  PATCH /io_limit_rule/name:<n>
  PATCH /volume/<id>            (qos_performance_policy_id)

Supported specs (see PowerStore driver documentation)
-----------------------------------------------------
  bandwidth_limit_type  Absolute | Density  (mandatory)
  max_iops              1 .. 2147483646
  max_bw                2000 .. 2147483646   (KB/s, or KB/s per GB)
  burst_percentage      0 .. 100             (default 0)
  At least one of max_iops / max_bw is required.

Running (plugin need not be pip-installed)::

    cd /opt/stack/tempest
    PYTHONPATH=/opt/stack/tempest/dell_tempest_plugin \\
    TEMPEST_CONFIG_DIR=/opt/stack/tempest/etc \\
    /opt/stack/data/venv/bin/python -m testtools.run \\
      dell_tempest_plugin.tests.powerstore_cinder.test_powerstore_qos
"""

import configparser
import json
import threading
import time
import uuid

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

QOS_MIN_ARRAY_VERSION = (4, 0)
IOPS_MIN, IOPS_MAX = 1, 2147483646
BW_MIN, BW_MAX = 2000, 2147483646
BURST_MIN, BURST_MAX = 0, 100

# Errors surfaced by the Cinder API when the driver fails initialize_connection
ATTACH_FAILURES = (lib_exc.ServerFault, lib_exc.BadRequest,
                   lib_exc.UnexpectedResponseCode, lib_exc.Conflict)

requests.packages.urllib3.disable_warnings()


# ======================================================================
# Base mixin - helpers only, no test methods
# ======================================================================
class PowerStoreQoSBase(object):
    """Helpers for PowerStore QoS functional tests."""

    BACKEND_NAME = 'powerstore1'

    # Objects created by *this* process (each stestr worker is its own
    # process, so parallel workers never sweep each other's objects).
    _created = {'volumes': [], 'types': [], 'qos': [], 'hosts': []}

    @classmethod
    def resource_cleanup(cls):
        try:
            cls._sweep_leftovers()
        finally:
            super(PowerStoreQoSBase, cls).resource_cleanup()

    @classmethod
    def _sweep_leftovers(cls):
        """Safety net: remove anything this process created that survived.

        Per-test addCleanup handlers normally remove everything; this only
        matters after a failed/aborted cleanup. Only ids recorded by this
        process are touched.
        """
        reg = cls._created
        vols = cls.os_admin.volumes_client_latest
        for vol_id in reg['volumes']:
            try:
                vols.delete_volume(vol_id)
                vols.wait_for_resource_deletion(vol_id)
            except lib_exc.NotFound:
                pass
            except Exception as e:
                LOG.warning("sweep: volume %s: %s", vol_id, e)
        types = cls.os_admin.volume_types_client_latest
        for type_id in reg['types']:
            try:
                types.delete_volume_type(type_id)
            except lib_exc.NotFound:
                pass
            except Exception as e:
                LOG.warning("sweep: volume type %s: %s", type_id, e)
        qos = cls.os_admin.volume_qos_client_latest
        for qos_id in reg['qos']:
            try:
                qos.delete_qos(qos_id, force=True)
            except lib_exc.NotFound:
                pass
            except Exception as e:
                LOG.warning("sweep: qos %s: %s", qos_id, e)
        ps_ip, ps_user, ps_pass = cls._read_powerstore_credentials()
        if not ps_ip:
            return

        def _ps(method, path, **kw):
            return requests.request(
                method, 'https://%s/api/rest%s' % (ps_ip, path),
                auth=(ps_user, ps_pass), verify=False, timeout=60, **kw)

        def _delete_by_name(kind, name):
            r = _ps('GET', kind, params={'name': 'eq.%s' % name,
                                         'select': 'id'})
            if r.status_code == 200 and r.json():
                _ps('DELETE', '%s/%s' % (kind, r.json()[0]['id']))

        try:
            for qos_id in reg['qos']:
                _delete_by_name('/policy', 'qos-policy-%s' % qos_id)
                _delete_by_name('/io_limit_rule', 'io-rule-%s' % qos_id)
            for host in reg['hosts']:
                _delete_by_name('/host', '%s-iSCSI' % host)
        except Exception as e:
            LOG.warning("sweep: PowerStore cleanup failed: %s", e)
        for key in reg:
            del reg[key][:]

    # ------------------------------------------------------------------
    # Setup / discovery
    # ------------------------------------------------------------------
    def setUp(self):
        super(PowerStoreQoSBase, self).setUp()
        self.vols = self.admin_volume_client
        self.vtypes = self.admin_volume_types_client
        self.qos = self.admin_volume_qos_client
        self.snaps = self.os_admin.snapshots_client_latest

        self.ps_ip, self.ps_user, self.ps_pass = (
            self._read_powerstore_credentials())
        if not self.ps_ip:
            self.skipTest("PowerStore credentials for backend '%s' not found "
                          "in /etc/cinder/cinder.conf." % self.BACKEND_NAME)
        if not self._backend_is_up():
            self.skipTest("cinder-volume for backend '%s' is not up."
                          % self.BACKEND_NAME)
        version = self._ps_array_version()
        if version is None or version < QOS_MIN_ARRAY_VERSION:
            self.skipTest("PowerStore array version %s does not support QoS "
                          "(needs >= %s)." % (version, QOS_MIN_ARRAY_VERSION))
        if self._backend_protocol() not in (None, 'iSCSI'):
            self.skipTest("QoS tests build an iSCSI connector; backend "
                          "protocol is %s." % self._backend_protocol())

    @classmethod
    def _read_powerstore_credentials(cls):
        conf = configparser.ConfigParser(interpolation=None)
        conf.read('/etc/cinder/cinder.conf')
        for section in (cls.BACKEND_NAME, 'backend_defaults'):
            try:
                return (conf.get(section, 'san_ip'),
                        conf.get(section, 'san_login'),
                        conf.get(section, 'san_password'))
            except (configparser.NoSectionError, configparser.NoOptionError):
                continue
        return None, None, None

    def _backend_is_up(self):
        try:
            client = self.os_admin.volume_services_client_latest
            for svc in client.list_services()['services']:
                if (svc['binary'] == 'cinder-volume' and
                        svc['host'].endswith('@' + self.BACKEND_NAME)):
                    return svc['state'] == 'up' and svc['status'] == 'enabled'
        except Exception as e:
            LOG.warning("Service discovery failed: %s", e)
        return False

    def _backend_protocol(self):
        try:
            pools = self.os_admin.volume_scheduler_stats_client_latest.list_pools(
                detail=True)['pools']
            for p in pools:
                caps = p.get('capabilities') or {}
                if caps.get('volume_backend_name') == self.BACKEND_NAME:
                    return caps.get('storage_protocol')
        except Exception as e:
            LOG.warning("Pool discovery failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # PowerStore REST helpers
    # ------------------------------------------------------------------
    def _ps(self, method, path, **kwargs):
        return requests.request(
            method, 'https://%s/api/rest%s' % (self.ps_ip, path),
            auth=(self.ps_user, self.ps_pass), verify=False, timeout=60,
            **kwargs)

    def _ps_array_version(self):
        r = self._ps('GET', '/software_installed', params={
            'select': 'release_version', 'is_cluster': 'eq.True'})
        if r.status_code != 200 or not r.json():
            return None
        return tuple(int(p) for p in
                     r.json()[0]['release_version'].split('.')[:2])

    def _ps_first(self, path, params):
        r = self._ps('GET', path, params=params)
        self.assertEqual(200, r.status_code, r.text)
        data = r.json()
        return data[0] if data else None

    def _ps_policy(self, qos_id):
        return self._ps_first('/policy', {
            'name': 'eq.qos-policy-%s' % qos_id, 'type': 'eq.QoS',
            'select': 'id,name,type,managed_by'})

    def _ps_policies(self, qos_id):
        r = self._ps('GET', '/policy', params={
            'name': 'eq.qos-policy-%s' % qos_id, 'select': 'id,name'})
        self.assertEqual(200, r.status_code, r.text)
        return r.json()

    def _ps_io_rule(self, qos_id):
        return self._ps_first('/io_limit_rule', {
            'name': 'eq.io-rule-%s' % qos_id,
            'select': 'id,name,type,max_iops,max_bw,burst_percentage'})

    def _ps_volume(self, cinder_vol_id):
        return self._ps_first('/volume', {
            'name': 'eq.volume-%s' % cinder_vol_id,
            'select': 'id,name,qos_performance_policy_id'})

    def _ps_volume_policy_id(self, cinder_vol_id):
        vol = self._ps_volume(cinder_vol_id)
        self.assertIsNotNone(
            vol, "volume-%s not found on PowerStore" % cinder_vol_id)
        return vol.get('qos_performance_policy_id')

    def _ps_snapshot_of_array_qos_objects(self):
        """Every QoS policy/io_limit_rule not named by this driver.

        Objects named qos-policy-*/io-rule-* belong to Cinder (possibly to
        tests running in parallel) and are excluded.
        """
        pols = self._ps('GET', '/policy', params={
            'type': 'eq.QoS', 'select': 'id,name'}).json()
        rules = self._ps('GET', '/io_limit_rule', params={
            'select': 'id,name,type,max_iops,max_bw,burst_percentage'}).json()
        return (sorted((p['id'], p['name']) for p in pols
                       if not p['name'].startswith('qos-policy-')),
                sorted((r['id'], r['name'], r['type'], r['max_iops'],
                        r['max_bw'], r['burst_percentage']) for r in rules
                       if not r['name'].startswith('io-rule-')))

    def _ps_cleanup_qos_objects(self, qos_id):
        """Remove the policy/rule that the test itself created (by name)."""
        try:
            policy = self._ps_policy(qos_id)
            if policy:
                self._ps('DELETE', '/policy/%s' % policy['id'])
            rule = self._ps_io_rule(qos_id)
            if rule:
                self._ps('DELETE', '/io_limit_rule/%s' % rule['id'])
        except Exception as e:
            LOG.warning("PowerStore QoS cleanup for %s failed: %s", qos_id, e)

    def _ps_cleanup_host(self, host_name):
        """Remove the PowerStore host created for a fake test connector."""
        try:
            host = self._ps_first('/host', {
                'name': 'eq.%s-iSCSI' % host_name, 'select': 'id,name'})
            for _ in range(5):
                if not host:
                    break
                r = self._ps('DELETE', '/host/%s' % host['id'])
                if r.status_code in (200, 204, 404):
                    break
                time.sleep(2)
        except Exception as e:
            LOG.warning("PowerStore host cleanup for %s failed: %s",
                        host_name, e)

    # ------------------------------------------------------------------
    # QoS spec / volume type helpers
    # ------------------------------------------------------------------
    def _new_qos(self, consumer='back-end', **specs):
        """Create a Cinder QoS spec (cleaned up with its PowerStore objects).

        :param specs: QoS keys, values are converted to str.
        """
        name = data_utils.rand_name(prefix=CONF.resource_name_prefix,
                                    name='ps-qos')
        specs = {k: str(v) for k, v in specs.items()}
        q = self.qos.create_qos(name=name, consumer=consumer,
                                **specs)['qos_specs']
        self._created['qos'].append(q['id'])
        self.addCleanup(self._delete_qos_safe, q['id'])
        self.addCleanup(self._ps_cleanup_qos_objects, q['id'])
        return q

    def _delete_qos_safe(self, qos_id):
        try:
            self.qos.disassociate_all_qos(qos_id)
        except Exception:
            pass
        try:
            self.qos.delete_qos(qos_id, force=True)
        except lib_exc.NotFound:
            pass
        except Exception as e:
            LOG.warning("delete_qos(%s) failed: %s", qos_id, e)

    def _new_volume_type(self, qos=None, name='ps-qos-type'):
        vt = self.vtypes.create_volume_type(
            name=data_utils.rand_name(prefix=CONF.resource_name_prefix,
                                      name=name),
            extra_specs={'volume_backend_name': self.BACKEND_NAME}
        )['volume_type']
        self._created['types'].append(vt['id'])
        self.addCleanup(self._delete_volume_type_safe, vt['id'])
        if qos:
            self.qos.associate_qos(qos['id'], vt['id'])
        return vt

    def _delete_volume_type_safe(self, type_id, timeout=300, interval=5):
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.vtypes.delete_volume_type(type_id)
                return
            except lib_exc.BadRequest:
                time.sleep(interval)
            except lib_exc.NotFound:
                return
        LOG.warning("Volume type %s could not be deleted", type_id)

    def _qos_type(self, consumer='back-end', **specs):
        """QoS spec + associated volume type. Returns (qos, vtype)."""
        q = self._new_qos(consumer=consumer, **specs)
        return q, self._new_volume_type(qos=q)

    # ------------------------------------------------------------------
    # Volume / snapshot helpers
    # ------------------------------------------------------------------
    def _create_volume(self, vt, size=1, **kwargs):
        vol = self.vols.create_volume(
            name=data_utils.rand_name(prefix=CONF.resource_name_prefix,
                                      name='ps-qos-vol'),
            size=size, volume_type=vt['name'], **kwargs)['volume']
        self._created['volumes'].append(vol['id'])
        self.addCleanup(self._delete_volume_safe, vol['id'])
        waiters.wait_for_volume_resource_status(
            self.vols, vol['id'], 'available')
        return self.vols.show_volume(vol['id'])['volume']

    def _delete_volume_safe(self, vol_id):
        try:
            self.vols.delete_volume(vol_id)
        except lib_exc.NotFound:
            return
        except Exception as e:
            LOG.debug("delete_volume(%s) raised: %s", vol_id, e)
        try:
            self.vols.wait_for_resource_deletion(vol_id)
        except Exception:
            pass

    def _create_snapshot(self, vol_id):
        snap = self.snaps.create_snapshot(
            volume_id=vol_id,
            display_name=data_utils.rand_name(
                prefix=CONF.resource_name_prefix, name='ps-qos-snap'),
        )['snapshot']
        self.addCleanup(self._delete_snapshot_safe, snap['id'])
        waiters.wait_for_volume_resource_status(
            self.snaps, snap['id'], 'available')
        return snap

    def _delete_snapshot_safe(self, snap_id):
        try:
            self.snaps.delete_snapshot(snap_id)
            self.snaps.wait_for_resource_deletion(snap_id)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Attach / detach (driver initialize_connection / terminate_connection)
    # ------------------------------------------------------------------
    def _new_connector(self):
        suffix = uuid.uuid4().hex[:12]
        host = 'tempest-psqos-%s' % suffix
        self._created['hosts'].append(host)
        self.addCleanup(self._ps_cleanup_host, host)
        return {
            'host': host,
            'ip': '192.0.2.%d' % (int(suffix[:2], 16) % 250 + 1),
            'initiator': 'iqn.1994-05.com.tempest:psqos-%s' % suffix,
            'multipath': False,
            'platform': 'x86_64',
            'os_type': 'linux',
        }

    def _attach(self, vol, connector):
        """initialize_connection; registers a terminate cleanup."""
        body = self._initialize_connection(vol['id'], connector)
        self.addCleanup(self._detach_safe, vol['id'], connector)
        return body

    def _initialize_connection(self, vol_id, connector):
        """POST the os-initialize_connection volume action."""
        resp, body = self.vols.post(
            'volumes/%s/action' % vol_id,
            json.dumps({'os-initialize_connection': {'connector': connector}}))
        self.vols.expected_success(200, resp.status)
        return json.loads(body)['connection_info']

    def _detach(self, vol, connector):
        self.vols.terminate_connection(vol['id'], connector)

    def _detach_safe(self, vol_id, connector):
        try:
            self.vols.terminate_connection(vol_id, connector)
        except Exception as e:
            LOG.debug("terminate_connection(%s) raised: %s", vol_id, e)

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------
    def _assert_rule(self, qos, rule_type=None, max_iops=None, max_bw=None,
                     burst=0):
        rule = self._ps_io_rule(qos['id'])
        self.assertIsNotNone(
            rule, "io_limit_rule io-rule-%s missing on PowerStore"
                  % qos['id'])
        if rule_type:
            self.assertEqual(rule_type, rule['type'])
        self.assertEqual(max_iops, rule['max_iops'])
        self.assertEqual(max_bw, rule['max_bw'])
        self.assertEqual(burst, rule['burst_percentage'])
        return rule

    def _assert_attached_with_policy(self, vol, qos):
        policy = self._ps_policy(qos['id'])
        self.assertIsNotNone(
            policy, "QoS policy qos-policy-%s missing" % qos['id'])
        self.assertEqual(
            policy['id'], self._ps_volume_policy_id(vol['id']),
            "PowerStore volume is not bound to the expected QoS policy")
        return policy

    def _assert_no_policy(self, vol):
        self.assertIsNone(self._ps_volume_policy_id(vol['id']),
                          "PowerStore volume unexpectedly has a QoS policy")

    def _create_attach_check(self, rule_type, max_iops=None, max_bw=None,
                             burst=None, consumer='back-end'):
        """Create QoS + volume, attach, verify rule/policy, detach."""
        specs = {'bandwidth_limit_type': rule_type}
        if max_iops is not None:
            specs['max_iops'] = max_iops
        if max_bw is not None:
            specs['max_bw'] = max_bw
        if burst is not None:
            specs['burst_percentage'] = burst
        qos, vt = self._qos_type(consumer=consumer, **specs)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        self._assert_rule(qos, rule_type, max_iops, max_bw, burst or 0)
        self._assert_attached_with_policy(vol, qos)
        self._detach(vol, conn)
        self._assert_no_policy(vol)
        return qos, vt, vol


# ======================================================================
# Positive tests
# ======================================================================
class _PowerStoreQoSPositiveTests(object):

    # ---- spec combinations ------------------------------------------
    @decorators.idempotent_id('947af066-f713-462f-9642-b6391ebd3fb6')
    def test_qos_absolute_max_iops_only(self):
        """Absolute type with only max_iops creates rule+policy on attach."""
        self._create_attach_check('Absolute', max_iops=500)

    @decorators.idempotent_id('04f493a1-f02d-44d8-ac25-8e1821c9a161')
    def test_qos_absolute_max_bw_only(self):
        """Absolute type with only max_bw creates rule+policy on attach."""
        self._create_attach_check('Absolute', max_bw=50000)

    @decorators.idempotent_id('145340f7-6afa-44dd-a32f-3acd10ab24a7')
    def test_qos_absolute_iops_and_bw(self):
        """Absolute type with both max_iops and max_bw."""
        self._create_attach_check('Absolute', max_iops=1000, max_bw=64000)

    @decorators.idempotent_id('648c18a4-4b61-427c-80ca-93b4e2892007')
    def test_qos_density_iops_and_bw(self):
        """Density type (per-GB limits) with both max_iops and max_bw."""
        self._create_attach_check('Density', max_iops=100, max_bw=20000)

    @decorators.idempotent_id('a6757c60-9b1f-4423-b7f1-5fcbf6bc9f06')
    def test_qos_density_max_iops_only(self):
        """Density type with only max_iops."""
        self._create_attach_check('Density', max_iops=50)

    @decorators.idempotent_id('04f3d72b-5013-4aa9-94b4-e69cb9cb4546')
    def test_qos_burst_percentage_set(self):
        """burst_percentage is propagated to the io_limit_rule."""
        self._create_attach_check('Absolute', max_iops=800, burst=80)

    @decorators.idempotent_id('4ea875c1-b21d-4d8a-9b06-1ab6b592e651')
    def test_qos_burst_percentage_default_zero(self):
        """Omitted burst_percentage defaults to 0 on the array."""
        qos, _, _ = self._create_attach_check('Absolute', max_iops=800)
        self.assertEqual(0, self._ps_io_rule(qos['id'])['burst_percentage'])

    @decorators.idempotent_id('7922b746-096c-49a3-8fc3-6b7c78651f4f')
    def test_qos_missing_bandwidth_limit_type_accepted_by_array(self):
        """Without bandwidth_limit_type the driver sends type=None.

        The documentation calls the type mandatory, but the driver does not
        validate it; this records the array's actual behaviour: attach
        works and the rule is stored without a type (type is null).
        """
        qos, vt = self._qos_type(max_iops=100)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        rule = self._ps_io_rule(qos['id'])
        self.assertIsNone(rule['type'])
        self.assertEqual(100, rule['max_iops'])
        self._assert_attached_with_policy(vol, qos)

    # ---- boundaries --------------------------------------------------
    @decorators.idempotent_id('35b09334-fb58-4b6d-9f24-e92f15453ce4')
    def test_qos_min_boundaries(self):
        """Minimum values: max_iops=1, max_bw=2000, burst=0."""
        self._create_attach_check('Absolute', max_iops=IOPS_MIN,
                                  max_bw=BW_MIN, burst=BURST_MIN)

    @decorators.idempotent_id('916353a7-5167-479e-bbf9-1df6455bd264')
    def test_qos_max_boundaries(self):
        """Maximum values: max_iops=2147483646, max_bw=2147483646, burst=100."""
        self._create_attach_check('Absolute', max_iops=IOPS_MAX,
                                  max_bw=BW_MAX, burst=BURST_MAX)

    @decorators.idempotent_id('e60c9a5c-8ebe-4e91-ad34-ff1b90a8f78b')
    def test_qos_burst_boundaries(self):
        """burst_percentage at 0 and 100 are accepted."""
        for burst in (BURST_MIN, BURST_MAX):
            self._create_attach_check('Absolute', max_iops=300, burst=burst)

    # ---- consumer semantics -----------------------------------------
    @decorators.idempotent_id('e74de7ea-0d17-468b-8240-919b687b419e')
    def test_qos_consumer_both_applied(self):
        """consumer=both is enforced by the PowerStore back end."""
        self._create_attach_check('Absolute', max_iops=400, consumer='both')

    @decorators.idempotent_id('968aeada-0782-4c12-a365-312ed365f390')
    def test_qos_consumer_front_end_not_applied(self):
        """consumer=front-end must not create any PowerStore QoS object."""
        qos, vt = self._qos_type(
            consumer='front-end', bandwidth_limit_type='Absolute',
            max_iops=400)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        self.assertIsNone(self._ps_policy(qos['id']))
        self.assertIsNone(self._ps_io_rule(qos['id']))
        self._assert_no_policy(vol)

    @decorators.idempotent_id('e6c41389-a113-4a13-b282-0685be871a5b')
    def test_volume_type_without_qos_not_limited(self):
        """A volume type without QoS never gets a policy on attach."""
        vt = self._new_volume_type()
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        self._assert_no_policy(vol)
        self._detach(vol, conn)
        self._assert_no_policy(vol)

    # ---- apply/clear lifecycle --------------------------------------
    @decorators.idempotent_id('bed26e27-bfcd-43cf-86c2-a5f29f932f0f')
    def test_qos_not_applied_before_attach(self):
        """Creating a volume of a QoS type does not touch the array QoS."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=600)
        vol = self._create_volume(vt)
        self._assert_no_policy(vol)
        self.assertIsNone(self._ps_policy(qos['id']))
        self.assertIsNone(self._ps_io_rule(qos['id']))

    @decorators.idempotent_id('ba71d33c-944a-47e8-9ea4-823299ca3a90')
    def test_qos_policy_cleared_on_detach_but_objects_kept(self):
        """Detach unbinds the volume; the rule and policy stay for reuse."""
        qos, _, vol = self._create_attach_check('Absolute', max_iops=700)
        self.assertIsNotNone(self._ps_policy(qos['id']))
        self.assertIsNotNone(self._ps_io_rule(qos['id']))

    @decorators.idempotent_id('3b54ea3b-ad17-4a76-8e25-ea1bc3bd1272')
    def test_qos_reattach_reuses_policy(self):
        """Attach -> detach -> attach again reuses the same policy id."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=900)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        first = self._assert_attached_with_policy(vol, qos)['id']
        self._detach(vol, conn)
        self._assert_no_policy(vol)
        self._attach(vol, conn)
        second = self._assert_attached_with_policy(vol, qos)['id']
        self.assertEqual(first, second)
        self.assertEqual(1, len(self._ps_policies(qos['id'])))

    @decorators.idempotent_id('57f78b11-cbfb-47f9-9ea6-be800b5afc40')
    def test_qos_repeated_initialize_connection_idempotent(self):
        """Calling initialize_connection twice keeps one rule and policy."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=900, max_bw=10000)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        first = self._assert_attached_with_policy(vol, qos)['id']
        self._attach(vol, conn)
        self.assertEqual(
            first, self._assert_attached_with_policy(vol, qos)['id'])
        self.assertEqual(1, len(self._ps_policies(qos['id'])))
        self._assert_rule(qos, 'Absolute', 900, 10000)

    @decorators.idempotent_id('b22064f0-f89b-4819-a090-0127cd438ad8')
    def test_qos_attach_detach_cycles(self):
        """Several attach/detach cycles leave consistent array state."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1200)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        for _ in range(3):
            self._attach(vol, conn)
            self._assert_attached_with_policy(vol, qos)
            self._detach(vol, conn)
            self._assert_no_policy(vol)
        self.assertEqual(1, len(self._ps_policies(qos['id'])))

    # ---- shared / distinct policies ----------------------------------
    @decorators.idempotent_id('d30ff523-40b4-4996-ad58-a0dace97c10f')
    def test_two_volumes_same_qos_share_policy(self):
        """Volumes of one QoS spec share a single policy; detach is per vol."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1500)
        vol1 = self._create_volume(vt)
        vol2 = self._create_volume(vt)
        conn1, conn2 = self._new_connector(), self._new_connector()
        self._attach(vol1, conn1)
        self._attach(vol2, conn2)
        p1 = self._assert_attached_with_policy(vol1, qos)
        p2 = self._assert_attached_with_policy(vol2, qos)
        self.assertEqual(p1['id'], p2['id'])
        self.assertEqual(1, len(self._ps_policies(qos['id'])))
        self._detach(vol1, conn1)
        self._assert_no_policy(vol1)
        self._assert_attached_with_policy(vol2, qos)

    @decorators.idempotent_id('8fcf6b23-9fb8-4d19-bf2e-8a6da5338937')
    def test_two_volumes_same_host_same_qos(self):
        """Two volumes mapped to the same host both receive the policy."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1500)
        vol1 = self._create_volume(vt)
        vol2 = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol1, conn)
        self._attach(vol2, conn)
        self._assert_attached_with_policy(vol1, qos)
        self._assert_attached_with_policy(vol2, qos)

    @decorators.idempotent_id('ed6fb35c-68bd-44c0-a943-dfb9d54b844b')
    def test_distinct_qos_specs_get_distinct_policies(self):
        """Different QoS specs yield different rules, policies and limits."""
        qos1, vt1 = self._qos_type(bandwidth_limit_type='Absolute',
                                   max_iops=100)
        qos2, vt2 = self._qos_type(bandwidth_limit_type='Absolute',
                                   max_iops=200)
        vol1, vol2 = self._create_volume(vt1), self._create_volume(vt2)
        conn = self._new_connector()
        self._attach(vol1, conn)
        self._attach(vol2, conn)
        p1 = self._assert_attached_with_policy(vol1, qos1)
        p2 = self._assert_attached_with_policy(vol2, qos2)
        self.assertNotEqual(p1['id'], p2['id'])
        self._assert_rule(qos1, 'Absolute', 100)
        self._assert_rule(qos2, 'Absolute', 200)

    @decorators.idempotent_id('c0ee1e51-484a-4337-80ec-b889edb2d712')
    def test_concurrent_first_attach_creates_single_policy(self):
        """Parallel first attaches race to create rule/policy (retry path).

        The driver retries on DellPowerStoreQoSIORuleExists /
        DellPowerStoreQoSPolicyExists, so both volumes must end up bound to
        the same, single policy.
        """
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=2500)
        vols = [self._create_volume(vt) for _ in range(3)]
        conns = [self._new_connector() for _ in vols]
        errors = []

        def _go(vol, conn):
            try:
                self._initialize_connection(vol['id'], conn)
            except Exception as e:
                errors.append(e)

        for v, c in zip(vols, conns):
            self.addCleanup(self._detach_safe, v['id'], c)
        threads = [threading.Thread(target=_go, args=(v, c))
                   for v, c in zip(vols, conns)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([], errors, "concurrent attach failed: %s" % errors)
        self.assertEqual(1, len(self._ps_policies(qos['id'])))
        ids = {self._assert_attached_with_policy(v, qos)['id'] for v in vols}
        self.assertEqual(1, len(ids))

    # ---- QoS spec updates -------------------------------------------
    @decorators.idempotent_id('b9c23e19-c825-4b4e-ab94-0405b69c23d4')
    def test_qos_update_max_iops_applied_on_reattach(self):
        """Changing max_iops in Cinder updates the existing rule on attach."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=100)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        policy_id = self._assert_attached_with_policy(vol, qos)['id']
        self._assert_rule(qos, 'Absolute', 100)
        self._detach(vol, conn)

        self.qos.set_qos_key(qos['id'], max_iops='250')
        self._attach(vol, conn)
        self._assert_rule(qos, 'Absolute', 250)
        self.assertEqual(
            policy_id, self._assert_attached_with_policy(vol, qos)['id'])

    @decorators.idempotent_id('3ec5f2db-6ca9-4b3a-aac7-9c8e06cbe323')
    def test_qos_update_max_bw_and_burst_applied_on_reattach(self):
        """max_bw and burst_percentage changes reach the existing rule."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=100, max_bw=10000,
                                 burst_percentage=10)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        self._assert_rule(qos, 'Absolute', 100, 10000, 10)
        self._detach(vol, conn)

        self.qos.set_qos_key(qos['id'], max_bw='30000',
                             burst_percentage='60')
        self._attach(vol, conn)
        self._assert_rule(qos, 'Absolute', 100, 30000, 60)

    @decorators.idempotent_id('e4548810-991a-4b4e-ac73-bf31fee5c350')
    def test_qos_update_limit_type_applied_on_reattach(self):
        """Switching Absolute -> Density is applied to the existing rule."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=100)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        self._assert_rule(qos, 'Absolute', 100)
        self._detach(vol, conn)

        self.qos.set_qos_key(qos['id'], bandwidth_limit_type='Density')
        self._attach(vol, conn)
        self._assert_rule(qos, 'Density', 100)

    @decorators.idempotent_id('e93057ae-0b1e-40dc-97ff-11011fd4054a')
    def test_qos_update_visible_to_other_volume_of_same_type(self):
        """After a spec update, a new attach of another volume updates rule."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=100)
        vol1, vol2 = self._create_volume(vt), self._create_volume(vt)
        conn1, conn2 = self._new_connector(), self._new_connector()
        self._attach(vol1, conn1)
        self._assert_rule(qos, 'Absolute', 100)
        self.qos.set_qos_key(qos['id'], max_iops='333')
        self._attach(vol2, conn2)
        self._assert_rule(qos, 'Absolute', 333)
        self._assert_attached_with_policy(vol1, qos)
        self._assert_attached_with_policy(vol2, qos)

    # ---- other volume operations with QoS ---------------------------
    @decorators.idempotent_id('fa7c1150-6fe8-4776-b199-0d2f0db2c91e')
    def test_extend_volume_then_attach_keeps_qos(self):
        """A volume extended while detached still gets its QoS on attach."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1000)
        vol = self._create_volume(vt, size=1)
        self.vols.extend_volume(vol['id'], new_size=2)
        waiters.wait_for_volume_resource_status(
            self.vols, vol['id'], 'available')
        self.assertEqual(2, self.vols.show_volume(vol['id'])['volume']['size'])
        conn = self._new_connector()
        self._attach(vol, conn)
        self._assert_attached_with_policy(vol, qos)

    @decorators.idempotent_id('26e7fddb-1701-4811-945d-99ace32174ef')
    def test_extend_volume_after_qos_detach(self):
        """Extend after attach/detach of a QoS volume still works."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1000)
        vol = self._create_volume(vt, size=1)
        conn = self._new_connector()
        self._attach(vol, conn)
        self._detach(vol, conn)
        self.vols.extend_volume(vol['id'], new_size=2)
        waiters.wait_for_volume_resource_status(
            self.vols, vol['id'], 'available')
        self._attach(vol, conn)
        self._assert_attached_with_policy(vol, qos)

    @decorators.idempotent_id('509d8c28-19c5-4f44-85fd-20d6b19ec4f0')
    def test_snapshot_of_qos_volume_attach_keeps_qos(self):
        """Snapshotting a QoS volume does not disturb its QoS binding."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1000)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self._attach(vol, conn)
        self._create_snapshot(vol['id'])
        self._assert_attached_with_policy(vol, qos)

    @decorators.idempotent_id('f5c18282-3ca7-4c3d-b833-f5c5c45fc5ba')
    def test_volume_from_snapshot_gets_qos_on_attach(self):
        """A volume cloned from a snapshot with the QoS type gets the policy."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1000)
        src = self._create_volume(vt)
        snap = self._create_snapshot(src['id'])
        vol = self._create_volume(vt, snapshot_id=snap['id'])
        self._assert_no_policy(vol)
        conn = self._new_connector()
        self._attach(vol, conn)
        self._assert_attached_with_policy(vol, qos)

    @decorators.idempotent_id('e6d6a8be-cd71-4fbd-abba-10c4dc68376d')
    def test_cloned_volume_gets_qos_on_attach(self):
        """A cloned volume (source_volid) gets the policy on attach."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1000)
        src = self._create_volume(vt)
        clone = self._create_volume(vt, source_volid=src['id'])
        self._assert_no_policy(clone)
        conn = self._new_connector()
        self._attach(clone, conn)
        self._assert_attached_with_policy(clone, qos)

    @decorators.idempotent_id('15a154bc-a2e0-4bd3-8ed5-1eaeca5252ef')
    def test_delete_qos_volume_after_detach(self):
        """Deleting a detached QoS volume works and keeps the shared policy."""
        qos, vt = self._qos_type(bandwidth_limit_type='Absolute',
                                 max_iops=1000)
        vol = self._create_volume(vt)
        other = self._create_volume(vt)
        conn, conn2 = self._new_connector(), self._new_connector()
        self._attach(vol, conn)
        self._attach(other, conn2)
        self._detach(vol, conn)
        self.vols.delete_volume(vol['id'])
        self.vols.wait_for_resource_deletion(vol['id'])
        self._assert_attached_with_policy(other, qos)

    # ---- isolation ---------------------------------------------------
    @decorators.idempotent_id('96c13a2d-7652-4bc3-901c-3e861f646c72')
    def test_preexisting_array_qos_objects_untouched(self):
        """The driver only touches qos-policy-<id>/io-rule-<id> objects."""
        before = self._ps_snapshot_of_array_qos_objects()
        self._create_attach_check('Absolute', max_iops=1000, max_bw=5000,
                                  burst=20)
        after = self._ps_snapshot_of_array_qos_objects()
        for item in before[0]:
            self.assertIn(item, after[0])
        for item in before[1]:
            self.assertIn(item, after[1])


# ======================================================================
# Negative tests
# ======================================================================
class _PowerStoreQoSNegativeTests(object):

    def _assert_attach_rejected(self, consumer='back-end', **specs):
        """initialize_connection must fail and leave no QoS binding."""
        qos, vt = self._qos_type(consumer=consumer, **specs)
        vol = self._create_volume(vt)
        conn = self._new_connector()
        self.assertRaises(ATTACH_FAILURES, self._initialize_connection,
                          vol['id'], conn)
        self.addCleanup(self._detach_safe, vol['id'], conn)
        self._assert_no_policy(vol)
        self.assertEqual(
            'available', self.vols.show_volume(vol['id'])['volume']['status'])
        return qos, vol

    @decorators.idempotent_id('5259966b-4bc8-4a60-b7e0-d0f72cbd910f')
    def test_qos_invalid_bandwidth_limit_type_rejected(self):
        """An unknown bandwidth_limit_type is rejected by the array."""
        self._assert_attach_rejected(bandwidth_limit_type='Relative',
                                     max_iops=100)

    @decorators.idempotent_id('d3c55596-e708-4c66-974a-1a7dd080e2dc')
    def test_qos_without_iops_or_bw_rejected(self):
        """At least one of max_iops / max_bw is required."""
        self._assert_attach_rejected(bandwidth_limit_type='Absolute')

    @decorators.idempotent_id('5d6faf1f-ca93-45a8-aab7-753a687b54d0')
    def test_qos_max_iops_out_of_range_rejected(self):
        """max_iops below 1 or above 2147483646 is rejected."""
        for value in (IOPS_MIN - 1, IOPS_MAX + 1):
            self._assert_attach_rejected(bandwidth_limit_type='Absolute',
                                         max_iops=value)

    @decorators.idempotent_id('06d03f3e-dc01-4f81-81b5-213ce6ea96df')
    def test_qos_max_bw_out_of_range_rejected(self):
        """max_bw below 2000 or above 2147483646 is rejected."""
        for value in (BW_MIN - 1, BW_MAX + 1):
            self._assert_attach_rejected(bandwidth_limit_type='Absolute',
                                         max_bw=value)

    @decorators.idempotent_id('3464125d-3e27-4d26-88af-f854e9b3f1c5')
    def test_qos_burst_percentage_out_of_range_rejected(self):
        """burst_percentage above 100 is rejected."""
        self._assert_attach_rejected(bandwidth_limit_type='Absolute',
                                     max_iops=100,
                                     burst_percentage=BURST_MAX + 1)

    @decorators.idempotent_id('6ee9cbb3-3141-4383-820a-4715625b5e25')
    def test_qos_non_numeric_value_rejected(self):
        """A non-numeric max_iops fails in the driver before any array call."""
        qos, _ = self._assert_attach_rejected(
            bandwidth_limit_type='Absolute', max_iops='lots')
        self.assertIsNone(self._ps_io_rule(qos['id']))
        self.assertIsNone(self._ps_policy(qos['id']))

    @decorators.idempotent_id('34c1ff08-7ff4-4e7c-829e-afdbe7ed5978')
    def test_qos_attach_recovers_after_spec_fixed(self):
        """After an invalid spec is corrected, attach succeeds."""
        qos, vol = self._assert_attach_rejected(
            bandwidth_limit_type='Absolute', max_iops=IOPS_MAX + 1)
        self.qos.set_qos_key(qos['id'], max_iops='1000')
        conn = self._new_connector()
        self._attach(vol, conn)
        self._assert_rule(qos, 'Absolute', 1000)
        self._assert_attached_with_policy(vol, qos)


# ======================================================================
# Concrete test classes
# ======================================================================
class TestPowerStoreQoSPositive(
        _PowerStoreQoSPositiveTests,
        PowerStoreQoSBase,
        volume_base.BaseVolumeAdminTest):
    """Positive functional tests for PowerStore Cinder QoS."""
    credentials = ['primary', 'admin']


class TestPowerStoreQoSNegative(
        _PowerStoreQoSNegativeTests,
        PowerStoreQoSBase,
        volume_base.BaseVolumeAdminTest):
    """Negative functional tests for PowerStore Cinder QoS."""
    credentials = ['primary', 'admin']
