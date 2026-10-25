"""The test services the stack suites run beside the chart, as documents for Cluster.apply().

Their manifests are tests/k3s/*.yaml, where dependabot keeps the images
current. This module loads them and builds what depends on the suite: the
settings of a service as a ConfigMap, a NodePort for a port the host reaches,
and the toolbox pod.
"""

from __future__ import annotations

from pathlib import Path

import yaml

TESTS = Path(__file__).resolve().parents[1]
K3S = TESTS / "k3s"
ATTACHMENTS = "/var/www/MISP/app/attachments"


def load(name: str) -> list[dict]:
    """The documents of tests/k3s/<name>.yaml."""
    return [doc for doc in yaml.safe_load_all((K3S / f"{name}.yaml").read_text()) if doc]


def config_map(name: str, data: dict[str, str]) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}, "data": data}


def secret(name: str, data: dict[str, str]) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "stringData": data}


def nodeport(name: str, selector: str, ports: dict[int, int]) -> dict:
    """A NodePort Service to the pods named selector: {port: node port}. The cluster publishes each node port."""
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"test-{name}"},
            "spec": {"type": "NodePort", "selector": {"app.kubernetes.io/name": selector},
                     "ports": [{"name": f"p{port}", "port": port, "targetPort": port, "nodePort": node_port}
                               for port, node_port in ports.items()]}}


def garage() -> list[dict]:
    return load("garage")


def keycloak() -> list[dict]:
    return [config_map("keycloak-realm", {"misp-realm.json": (TESTS / "keycloak-realm.json").read_text()}),
            *load("keycloak")]


def image(name: str) -> str:
    """The image of the first container in tests/k3s/<name>.yaml."""
    workload = next(doc for doc in load(name) if doc["kind"] == "Deployment")
    return workload["spec"]["template"]["spec"]["containers"][0]["image"]


def postgres_tls(certificate: dict[str, str], user: dict[str, str]) -> list[dict]:
    """PostgreSQL that takes password logins over TLS only, with its Secrets as the Percona operator makes them.

    certificate holds ca.crt, tls.crt and tls.key; user holds host, port, dbname, user and password.
    """
    return [secret("pg-cluster-cert", certificate), secret("pg-pguser-misp", user), *load("postgres-tls")]


def external_redis(password: str) -> list[dict]:
    return [secret("external-redis", {"password": password}), *load("external-redis")]


def mariadb(user: str, password: str, database: str = "misp") -> list[dict]:
    return [config_map("mariadb-env", {"MARIADB_DATABASE": database, "MARIADB_USER": user,
                                       "MARIADB_PASSWORD": password}),
            *load("mariadb")]


def misp25(settings: dict[str, str]) -> list[dict]:
    """MISP 2.5 with settings in the names of MISP's own images, on mariadb()."""
    return [config_map("misp-25-env", settings), *load("misp-25")]


def toolbox(job: dict, container: str = "configure") -> dict:
    """A long-running pod from a container of a configure Job's pod template: its image, env and mounts.

    The suites run one-off commands here, as a Job of the chart would, with the
    attachments the workers share and no resource limits.
    """
    template = job["spec"]["template"]["spec"]
    source = next(c for c in [*template.get("initContainers", []), *template["containers"]] if c["name"] == container)
    mounts = source.get("volumeMounts", [])
    if not any(m["mountPath"] == ATTACHMENTS for m in mounts):
        mounts = [*mounts, {"name": "attachments", "mountPath": ATTACHMENTS}]
    box = {**{k: v for k, v in source.items() if k != "resources"}, "name": "toolbox", "args": ["sleep", "infinity"],
           "volumeMounts": mounts}
    spec = {k: v for k, v in template.items() if k not in ("initContainers", "containers", "restartPolicy")}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": "toolbox", "labels": {"app.kubernetes.io/name": "toolbox"}},
            "spec": {**spec, "containers": [box], "restartPolicy": "Always", "terminationGracePeriodSeconds": 1}}
