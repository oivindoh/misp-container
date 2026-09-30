"""The garage S3 store of the test stacks: layout, keys and buckets through its admin API.

tests/docker-compose.test.yml runs garage with the admin API on localhost:3903
and S3 on localhost:3900 (garage:3900 in the compose network).
"""

import json
import time
import urllib.request

ADMIN = "http://localhost:3903"
TOKEN = "s3cr3t-admin-t0ken"
S3_HOST_ENDPOINT = "http://localhost:3900"
REGION = "garage"


def call(method: str, path: str, data=None) -> dict:
    request = urllib.request.Request(ADMIN + path, method=method,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def layout(timeout: int = 30) -> None:
    """Give the single node a role, once garage answers."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            node = call("GET", "/v2/GetClusterStatus")["nodes"][0]["id"]
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    call("POST", "/v2/UpdateClusterLayout", {"roles": [{"id": node, "zone": "dc1", "capacity": 1073741824, "tags": []}]})
    call("POST", "/v2/ApplyClusterLayout", {"version": 1})


def key(name: str, access_key: str = "", secret_key: str = "") -> tuple[str, str]:
    """A new key, or the given one imported; returns (access key, secret key)."""
    if access_key:
        call("POST", "/v2/ImportKey", {"name": name, "accessKeyId": access_key, "secretAccessKey": secret_key})
        return access_key, secret_key
    created = call("POST", "/v2/CreateKey", {"name": name})
    return created["accessKeyId"], created["secretAccessKey"]


def bucket(alias: str, access_key: str) -> str:
    """A new bucket the key may read and write; returns its id."""
    bucket_id = call("POST", "/v2/CreateBucket", {"globalAlias": alias})["id"]
    call("POST", "/v2/AllowBucketKey", {"bucketId": bucket_id, "accessKeyId": access_key,
                                        "permissions": {"read": True, "write": True, "owner": True}})
    return bucket_id
