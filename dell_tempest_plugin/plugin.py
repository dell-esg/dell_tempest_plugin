import os
from oslo_config import cfg
from tempest.test_discover import plugins

# Define plugin-specific config options
volume_opts = [
    cfg.BoolOpt('replication',
                default=True,
                help='Enable replication tests for PowerStore'),
    cfg.BoolOpt('volume_types',
                default=True,
                help='Enable volume type tests'),
]

class DellTempestPlugin(plugins.TempestPlugin):

    def get_opt_lists(self):
        # Register options under the 'powerstore' group        
        return [
                    ('service_available', [
                        cfg.BoolOpt('cinder', default=True,
                                    help='Whether or not cinder is expected to be available'),
                    ]),
                    ('volume-feature-enabled', volume_opts),
                    ('volume', [
                        cfg.StrOpt('catalog_type', default='block-storage',
                                help='Catalog type of the Volume service'),
                        cfg.StrOpt('endpoint_type', default='public',
                                help='Endpoint type to use for the Volume service'),
                        cfg.StrOpt('region', default='RegionOne',
                                help='Region for the Volume service endpoint'),
                    ]),
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
    
    
    def get_test_paths(self):
        driver = os.getenv('DELL_DRIVER', 'all')
        base_path = os.path.dirname(os.path.abspath(__file__))

        if driver == 'powerstore':
            return [os.path.join(base_path, 'tests', 'powerstore')]
        elif driver == 'powerflex':
            return [os.path.join(base_path, 'tests', 'powerflex')]
        else:
            return [os.path.join(base_path, 'tests')]

    def get_tempest_plugins(self):
        return []


    def load_tests(self):
        base_path = os.path.split(os.path.dirname(os.path.abspath(__file__)))[0]
        test_dir = "dell_tempest_plugin"
        driver = os.getenv('DELL_DRIVER', 'all')

        if driver == 'powerstore':
            full_test_dir = os.path.join(base_path, test_dir, 'tests', 'powerstore')
        elif driver == 'powerflex':
            full_test_dir = os.path.join(base_path, test_dir, 'tests', 'powerflex')
        else:
            full_test_dir = os.path.join(base_path, test_dir, 'tests')

        return full_test_dir, base_path


    def get_metadata(self):
        return {
            'display_name': 'Dell PowerStore Tempest Plugin',
            'description': 'Tempest tests for Dell EMC PowerStore Cinder driver',
            'maintainer': 'Dell EMC OpenStack Team',
        }


    def register_opts(self, conf):
        conf.register_opts(volume_opts, group='volume-feature-enabled')
