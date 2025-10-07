from oslo_config import cfg
from dell_tempest_plugin.services.failover_client import DellFailoverClient
from tempest.test_discover import plugins

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
        return (
            'dell_tempest_plugin.tests',
            'dell_tempest_plugin.tests.api',
            'dell_tempest_plugin.tests.scenario',
        )


    def get_metadata(self):
        return {
            'display_name': 'Dell PowerStore Tempest Plugin',
            'description': 'Tempest tests for Dell EMC PowerStore Cinder driver',
            'maintainer': 'Dell EMC OpenStack Team',
        }


    def register_opts(self, conf):
        conf.register_opts(powerstore_opts, group='volume-feature-enabled')
