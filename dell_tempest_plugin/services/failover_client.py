import json
from tempest.lib.services.volume.v3.services_client import ServicesClient
from tempest.lib import exceptions as lib_exc

class DellFailoverClient(ServicesClient):

    def failover_host(self, host: str, backend_id: str | None = None):
        url = 'os-services/failover-host'      # relative v3 path
        headers = {'Content-Type': 'application/json'}
        body = {'host': host}
        if backend_id is not None:
            body['backend_id'] = backend_id

        # POST /v3/{project_id}/os-services/failover-host
        # BaseClient will prefix with /v3/{project_id}
        resp, resp_body = self.post(url, headers=headers, body=json.dumps(body))
        # Expected: 202 Accepted (operation is async)
        if not (200 <= resp.status <= 299):
            raise lib_exc.TempestException(
                f"Failover-host returned HTTP {resp.status}: {resp_body!r}"
            )
        return resp, resp_body
