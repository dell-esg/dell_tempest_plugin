import os
import logging
from oslo_config import cfg
from tempest.test_discover import plugins
from dell_tempest_plugin import config

# Define plugin-specific config options
volume_opts = [
    cfg.BoolOpt('replication',
                default=True,
                help='Enable replication tests for PowerStore'),
    cfg.BoolOpt('volume_types',
                default=True,
                help='Enable volume type tests'),
]

LOG = logging.getLogger(__name__)

class DellTempestPlugin(plugins.TempestPlugin):

    def get_opt_lists(self):
        # Register options under the 'powerstore' group        
        return [
                    ('service_available', [
                        cfg.BoolOpt('cinder', default=True,
                                    help='Whether or not cinder is expected to be available'),
                        cfg.BoolOpt('manila', default=True,
                                    help='Whether or not manila is expected to be available'),
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
    
    
    def _get_driver(self):
        """Safely read dell_driver.driver from config, default to 'all'."""
        try:
            CONF = cfg.CONF
            driver = CONF.dell_driver.driver
        except (cfg.NoSuchGroupError, cfg.NoSuchOptError):
            driver = 'all'
        return driver if driver else 'all'

    def _get_all_test_dirs(self, base_path):
        """Return all backend test subdirectories that contain test files."""
        tests_root = os.path.join(base_path, 'tests')
        dirs = []
        if not os.path.isdir(tests_root):
            return [tests_root]
        for entry in sorted(os.listdir(tests_root)):
            sub = os.path.join(tests_root, entry)
            if os.path.isdir(sub) and not entry.startswith(('__', '.', 'base')):
                dirs.append(sub)
        return dirs if dirs else [tests_root]

    def get_test_paths(self):
        driver = self._get_driver()
        LOG.info(f"DELL_DRIVER in plugin: {driver}")

        base_path = os.path.dirname(os.path.abspath(__file__))

        if driver == 'all':
            return self._get_all_test_dirs(base_path)

        # Dynamic: use the driver name as the test subdirectory
        driver_test_dir = os.path.join(base_path, 'tests', driver)
        if os.path.isdir(driver_test_dir):
            return [driver_test_dir]

        LOG.warning(f"No test directory found for driver '{driver}', "
                    f"falling back to all test directories")
        return self._get_all_test_dirs(base_path)

    def get_tempest_plugins(self):
        return []


    def load_tests(self):
        base_path = os.path.split(os.path.dirname(os.path.abspath(__file__)))[0]
        test_dir = "dell_tempest_plugin"

        driver = self._get_driver()
        LOG.info(f"DELL_DRIVER in load_tests: {driver}")

        if driver != 'all':
            candidate = os.path.join(base_path, test_dir, 'tests', driver)
            if os.path.isdir(candidate):
                return candidate, base_path
            LOG.warning(f"No test directory for driver '{driver}', "
                        f"falling back to base tests directory")

        full_test_dir = os.path.join(base_path, test_dir, 'tests')
        return full_test_dir, base_path


    def get_metadata(self):
        return {
            'display_name': 'Dell Tempest Plugin',
            'description': 'Tempest tests for Dell EMC storage drivers (PowerStore, PowerFlex, PowerScale, PowerMax, Unity)',
            'maintainer': 'Dell EMC OpenStack Team',
        }


    def register_opts(self, conf):
        conf.register_opts(volume_opts, group='volume-feature-enabled')
