import logging
import dell_tempest_plugin.tests.api.test_dell_base as dell_base
import uuid
from tempest.lib import decorators

LOG = logging.getLogger(__name__)

class PowerStoreTempestTest(dell_base.BaseTempestTest):
    backend_name = "powerstore"
    backend_id = "powerstore-backend-id"

    @decorators.idempotent_id('328faacf-1dcc-40bc-a92c-92a9b5a1c4fe')
    def test_failover_host(self):
        LOG.info("Executing: PowerStoreTempestTest.test_failover_host")
        if not getattr(dell_base.CONF.volume_feature_enabled, 'replication', False):
            self.skipTest("Skipping test: replication not enabled")
        self._run_failover_test()