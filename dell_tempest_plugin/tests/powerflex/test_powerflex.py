import logging
import dell_tempest_plugin.tests.base.test_dell_base as dell_base
from tempest.lib import decorators

LOG = logging.getLogger(__name__)

class PowerflexTempestTest(dell_base.BaseTempestTest):
    backend_name = "powerflex"
    backend_id = "powerflex-backend-id"

    @decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef1234567890')
    def test_create_volume_with_volume_type(self):
        LOG.info("Executing: PowerflexTempestTest.test_create_volume_with_volume_type")
        # raise exception and skip check if feature is not enabled
        if not getattr(dell_base.CONF.volume_feature_enabled, 'volume_types', False):
            raise self.skipException("Volume types are not enabled")
        self._run_create_volume_with_volume_type()