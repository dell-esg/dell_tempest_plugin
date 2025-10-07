import json
from tempest.lib.services.volume.v3.services_client import ServicesClient

class DellFailoverClient(ServicesClient):

    def failover_host(self, host_name, backend_id=None):
        post_body = {
            'failover_host': {
                'backend_id': backend_id
            }
        }
        url = 'os-volume-hosts/%s/action' % host_name
        headers = {'Content-Type': 'application/json'}
        resp, body = self.post(url, headers=headers, body=json.dumps(post_body))
        return body
