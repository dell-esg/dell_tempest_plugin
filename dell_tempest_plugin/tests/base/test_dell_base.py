import logging

from cinder_tempest_plugin.api.volume import base as cinder_base
from dell_tempest_plugin.services.failover_client import DellFailoverClient
from tempest import clients, config
from tempest.common import credentials_factory, waiters
from tempest.lib.common.utils import data_utils
from tempest.lib import exceptions
from tempest.lib.services.volume.v3.services_client import ServicesClient
from tempest.lib.services.volume.v3.types_client import TypesClient
from tempest.lib.services.volume.v3.volumes_client import VolumesClient
from tempest.lib.services.volume.v3.qos_client import QosSpecsClient


CONF = config.CONF
LOG = logging.getLogger(__name__)
LOG.info("CONF.volume_feature_enabled.volume_types = %s", CONF.volume_feature_enabled.volume_types)


class BaseTempestTest(cinder_base.BaseVolumeTest):
    backend_name = None
    backend_id = None
    
    @classmethod    
    def setup_clients(cls):
        super(BaseTempestTest, cls).setup_clients()

        admin_creds = credentials_factory.get_configured_admin_credentials()
        cls.admin_manager = clients.Manager(credentials=admin_creds)

        # Use CONF.volume.catalog_type consistently (e.g., 'volumev3' or 'block-storage')
        service_type = CONF.volume.catalog_type
        region = CONF.volume.region or CONF.identity.region
        endpoint_type = CONF.volume.endpoint_type

        cls.volume_types_client = TypesClient(
            auth_provider=cls.admin_manager.auth_provider,
            service=service_type,
            region=region,
            endpoint_type=endpoint_type,
            build_interval=CONF.volume.build_interval,
            build_timeout=CONF.volume.build_timeout
        )

        cls.qos_client = QosSpecsClient(
            auth_provider=cls.admin_manager.auth_provider,
            service=service_type,
            region=region
        )

        cls.volumes_client = VolumesClient(
            auth_provider=cls.admin_manager.auth_provider,
            service=service_type,
            region=region,
            endpoint_type=endpoint_type,
            build_interval=CONF.volume.build_interval,
            build_timeout=CONF.volume.build_timeout
        )

        cls.volume_services_client = ServicesClient(
            auth_provider=cls.admin_manager.auth_provider,
            service=service_type,  # <-- Was 'volume'; use catalog_type instead
            region=region,
            endpoint_type=endpoint_type,
            build_interval=CONF.volume.build_interval,
            build_timeout=CONF.volume.build_timeout
        )

        cls.failover_client = DellFailoverClient(
            auth_provider=cls.admin_manager.auth_provider,
            service=service_type,
            region=region,
            endpoint_type=endpoint_type,
            build_interval=CONF.volume.build_interval,
            build_timeout=CONF.volume.build_timeout
        )


    @classmethod
    def skip_checks(cls):
        super(BaseTempestTest, cls).skip_checks()
        if not getattr(CONF.volume_feature_enabled, 'replication', False):
            raise cls.skipException("Replication not enabled")

    def discover_host_name(self):
        try:
            services = self.volume_services_client.list_services()['services']
            for svc in services:
                if svc['binary'] == 'cinder-volume' and self.backend_name in svc['host']:
                    return svc['host']
        except Exception as e:
            self.fail(f"Failed to discover volume host for failover: {e}")
        self.fail(f"No matching host found for backend: {self.backend_name}")

    def safe_delete_volume(self, volume_id):
        try:
            volume = self.volumes_client.show_volume(volume_id)['volume']
            deletable_states = ['available',
                                'error',
                                'error_restoring',
                                'error_extending',
                                'error_managing']
            if volume['status'] in deletable_states:
                self.volumes_client.delete_volume(volume_id)
                waiters.wait_for_volume_resource_status(self.volumes_client,
                                                        volume_id,
                                                        'deleted')
            else:
                LOG.warning("Skipping deletion of volume %s due to non-deletable status: %s",
                            volume_id, volume['status'])
        except Exception as e:
            LOG.error("Exception during volume cleanup for %s: %s",
                      volume_id, str(e))

    def _run_failover_test(self):
        volume_name = data_utils.rand_name(f"{self.backend_name}-volume")

        try:
            volume = self.create_volume(name=volume_name, replication=True)
        except Exception as e:
            self.fail(f"Failed to create volume with replication: {e}")

        self.assertIsNotNone(volume,
                             "create_volume returned None")
        self.assertIn('id',
                      volume,
                      "Created volume does not have an 'id'")
        self.assertTrue(volume.get('id'),
                        "Volume ID is empty or missing")

        waiters.wait_for_volume_resource_status(self.volumes_client,
                                                volume['id'],
                                                'available')
        volume = self.volumes_client.show_volume(volume['id'])['volume']

        self.addCleanup(self.safe_delete_volume,
                        volume['id'])

        if volume['status'] != 'available':
            self.fail(f"Volume not in 'available' state after creation: {volume['status']}")

        LOG.info("Created volume %s with replication", volume['id'])

        host_name = self.discover_host_name()

        try:
            LOG.info("Triggering failover for host: %s with backend: %s",
                     host_name, self.backend_id)
            self.failover_client.failover_host(host_name,
                                               backend_id=self.backend_id)
        except Exception as e:
            self.fail(f"Failover operation failed: {e}")

        try:
            volume_details = self.volumes_client.show_volume(volume['id'])['volume']
            LOG.info("Volume details after failover: %s", volume_details)
        except Exception as e:
            self.fail(f"Failed to fetch volume details after failover: {e}")

        self.assertEqual(volume_details['status'],
                         'available',
                         f"Volume not available after failover: {volume_details['status']}")
        self.assertIn('replication_status',
                      volume_details,
                      "Missing replication_status in volume details")
        self.assertEqual(volume_details['replication_status'],
                         'failed-over',
                         f"Unexpected replication_status: {volume_details['replication_status']}")
    
    #@decorators.idempotent_id('a1b2c3d4-e5f6-7890-abcd-ef1234567890')
    def _run_create_volume_with_volume_type(self):
        # Step 1: Create volume type
        volume_type_name = data_utils.rand_name("powerflex_limit_iops")
        volume_type = self.volume_types_client.create_volume_type(
            name=volume_type_name,
            extra_specs={
                "volume_backend_name": "powerflex1",
                "powerflex:storage_pool_name": "SP1",
                "powerflex:protection_domain_name": "PD1",
                "provisioning:type": "thin",
                "powerflex:iops_limit": "5000"
            }
        )['volume_type']

        self.assertEqual(volume_type['name'], volume_type_name)
        self.assertTrue(volume_type['is_public'])
        self.assertIn("volume_backend_name", volume_type['extra_specs'])
        self.assertEqual(volume_type['extra_specs']['volume_backend_name'], "powerflex1")

        # Step 2: Create volume using the volume type
        volume_name = data_utils.rand_name("powerflex_volume")
        volume = self.volumes_client.create_volume(
            size=8,
            volume_type=volume_type_name,
            name=volume_name
        )['volume']

        # Wait for volume to become available        
        waiters.wait_for_volume_resource_status(
            self.volumes_client,
            volume['id'],
            'available'
        )

        # Validate volume properties
        volume_details = self.volumes_client.show_volume(volume['id'])['volume']
        self.assertEqual(volume_details['name'], volume_name)
        self.assertEqual(volume_details['volume_type'], volume_type_name)
        self.assertEqual(volume_details['size'], 8)

        # Step 3: Cleanup volume and wait for deletion
        self.volumes_client.delete_volume(volume['id'])
        
        try:
            waiters.wait_for_volume_resource_status(
                self.volumes_client,
                volume['id'],
                'deleted'
            )
        except exceptions.NotFound:
            # Volume is already deleted, which is acceptable
            pass

        # Step 4: Cleanup volume type
        self.volume_types_client.delete_volume_type(volume_type['id'])
    def _run_create_volume_with_qos_spec(self):

        qos_name = data_utils.rand_name("powerflex_qos")

        # Step 1: Create QoS spec
        qos_specs = self.qos_client.create_qos(
            name=qos_name,
            consumer='back-end'
        )['qos_specs']

        # Register cleanup for QoS spec with disassociation
        def cleanup_qos():
            try:
                self.qos_client.disassociate_qos(qos_specs['id'], volume_type['id'])
            except Exception:
                pass  # Ignore if already disassociated or not found
            try:
                self.qos_client.delete_qos(qos_specs['id'])
            except exceptions.NotFound:
                pass  # Already deleted

        self.addCleanup(cleanup_qos)

        # Step 2: Set QoS keys correctly
        self.qos_client.set_qos_key(
            qos_specs['id'],
            **{'powerflex:iops_limit': '5000'}
        )
        
        # Step 3: Create volume type
        volume_type_name = data_utils.rand_name("powerflex_limit_iops")
        volume_type = self.volume_types_client.create_volume_type(
            name=volume_type_name,
            extra_specs={
                "volume_backend_name": "powerflex1",
                "powerflex:storage_pool_name": "SP1",
                "powerflex:protection_domain_name": "PD1",
                "provisioning:type": "thin"
            }
        )['volume_type']
        self.addCleanup(self.volume_types_client.delete_volume_type, volume_type['id'])

        # Associate QoS with volume type
        self.qos_client.associate_qos(qos_specs['id'], volume_type['id'])

        self.assertEqual(volume_type['name'], volume_type_name)
        self.assertTrue(volume_type['is_public'])
        self.assertIn("volume_backend_name", volume_type['extra_specs'])
        self.assertEqual(volume_type['extra_specs']['volume_backend_name'], "powerflex1")

        # Step 4: Create volume using the volume type
        volume_name = data_utils.rand_name("powerflex_volume")
        volume = self.volumes_client.create_volume(
            size=8,
            volume_type=volume_type_name,
            name=volume_name
        )['volume']


        # Wait for volume to become available        
        waiters.wait_for_volume_resource_status(
            self.volumes_client,
            volume['id'],
            'available'
        )

        # Validate volume properties
        volume_details = self.volumes_client.show_volume(volume['id'])['volume']
        self.assertEqual(volume_details['name'], volume_name)
        self.assertEqual(volume_details['volume_type'], volume_type_name)
        self.assertEqual(volume_details['size'], 8)

        # Step 5: Cleanup volume and wait for deletion
        try:
            self.volumes_client.delete_volume(volume['id'])
            waiters.wait_for_volume_resource_status(
                self.volumes_client,
                volume['id'],
                'deleted'
            )
        except exceptions.NotFound:
            # Volume is already deleted, which is acceptable
            pass