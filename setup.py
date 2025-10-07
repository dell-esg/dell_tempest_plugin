from setuptools import setup, find_packages

setup(
    name='DellTempestPlugin',
    version='0.1',
    description='Tempest plugin to test Dell PowerStore Cinder failover_host',
    author='Prasant Padhi',
    packages=find_packages(),
    include_package_data=True,
    entry_points={
        'tempest.test_plugins': [
            'dell_tempest_plugin = dell_tempest_plugin.plugin:DellTempestPlugin'
        ]
    }
)
