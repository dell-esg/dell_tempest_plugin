import os
from oslo_config import cfg
from tempest.test_discover import plugins
import logging

LOG = logging.getLogger(__name__)

# Define plugin-specific config options
powerstore_opts = [
    cfg.BoolOpt('replication',
                default=True,
                help='Enable replication tests for PowerStore'),
]

class DellTempestPlugin(plugins.TempestPlugin):

    def get_opt_lists(self):
        # Register options under the 'powerstore' group
        return [
            ('volume', cfg.CONF.volume),
            ('volume-feature-enabled', powerstore_opts),
            ]


    def get_service_clients(self):
        return [
            {
                'name': 'powerstore_failover',
                'service_version': 'volume',
                'module_path': 'dell_tempest_plugin.services.failover_client',
                'client_names': ['DellFailoverClient'],
            }
        ]


    def get_tests_dirs(self):
        return ['dell_tempest_plugin/tests']

    def get_tempest_plugins(self):
        return []


    def load_tests(self):
        
        LOG.info("DellTempestPlugin: load_tests() called")
        LOG.info(f"Returning test_dir and top_path: {os.path.dirname(__file__)}")

        return (
            'dell_tempest_plugin.tests',  # Base directory for tests
            os.path.dirname(__file__)     # Plugin root path
        )


    def get_metadata(self):
        return {
            'display_name': 'Dell PowerStore Tempest Plugin',
            'description': 'Tempest tests for Dell EMC PowerStore Cinder driver',
            'maintainer': 'Dell EMC OpenStack Team',
        }


    def register_opts(self, conf):
        conf.register_opts(powerstore_opts, group='volume-feature-enabled')
