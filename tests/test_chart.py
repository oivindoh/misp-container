"""The Helm chart renders what the code and the release expect (helm template, no cluster)."""

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "files"))
sys.path.insert(0, str(REPO / "scripts"))

import chart  # noqa: E402
from misp_container import task  # noqa: E402

# Pods that run MISP and read misp-env
MISP_DEPLOYMENTS = ("web", "worker", "metrics")


def kinds(*sets: str, every_component: bool = False) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for doc in chart.objects(*sets, every_component=every_component):
        found.setdefault(doc["kind"], []).append(doc)
    return found


def env_data(*sets: str) -> dict[str, str]:
    return chart.find("ConfigMap", "misp-env", *sets)["data"]


def test_every_task_has_a_cronjobs_entry_and_each_scheduled_one_a_cronjob():
    # workflow runs per workflow ID (cronjobs.workflows)
    tasks = yaml.safe_load((chart.CHART / "values.yaml").read_text())["cronjobs"]["tasks"]
    assert set(tasks) == set(task.TASKS) - {task.WORKFLOW_TASK}
    commands = [chart.pod_spec(d)["containers"][0]["args"] for d in kinds(every_component=True)["CronJob"]]
    scheduled = {c[c.index("misp_container.task") + 1] for c in commands if "misp_container.task" in c}
    assert scheduled == {name for name, schedule in tasks.items() if schedule}


def test_every_task_cronjob_renders_app_config_and_mounts_the_attachments():
    for cronjob in kinds(every_component=True)["CronJob"]:
        if "misp_container.task" not in chart.pod_spec(cronjob)["containers"][0]["args"]:
            continue
        assert cronjob["metadata"]["labels"]["app.kubernetes.io/component"] == "misp-task"
        mounts = {m["mountPath"] for m in chart.pod_spec(cronjob)["containers"][0]["volumeMounts"]}
        assert {"/var/www/MISP/app/Config", "/var/www/MISP/app/attachments"} <= mounts, cronjob["metadata"]["name"]


def test_a_workflow_cronjob_passes_its_id():
    cronjob = chart.find("CronJob", "workflow-7", "cronjobs.enabled=true", "cronjobs.workflows.7=0 4 * * *")
    assert chart.pod_spec(cronjob)["containers"][0]["args"][-2:] == ["workflow", "7"]


def test_the_chart_version_is_a_release_or_a_wip_pre_release():
    # Helm skips a pre-release unless asked for it by version: only the wip pre-releases of a MISP pin are one
    assert re.fullmatch(r"\d+\.\d+\.\d+(-wip\.\d+)?", yaml.safe_load((chart.CHART / "Chart.yaml").read_text())["version"])


def test_every_image_of_ours_carries_the_app_version():
    version = yaml.safe_load((chart.CHART / "Chart.yaml").read_text())["appVersion"]
    rendered = yaml.safe_dump(chart.objects(every_component=True))
    ours = set(re.findall(r"image: (ghcr\.io/oivindoh/misp-container[\w-]*:\S+)", rendered))
    assert ours and all(image.endswith(f":{version}") for image in ours), ours


def test_the_jobs_are_per_revision_and_argo_cd_hooks():
    for job in kinds()["Job"]:
        assert re.fullmatch(r"(configure|org-sync)-1", job["metadata"]["name"]), job["metadata"]["name"]
        assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
        assert "ttlSecondsAfterFinished" not in job["spec"]


def test_a_change_to_env_or_secrets_rolls_the_misp_pods():
    before = {name: chart.find("Deployment", name)["spec"]["template"]["metadata"]["annotations"] for name in MISP_DEPLOYMENTS}
    env = {name: chart.find("Deployment", name, "env.DEBUG=1")["spec"]["template"]["metadata"]["annotations"] for name in MISP_DEPLOYMENTS}
    secret = {name: chart.find("Deployment", name, "secrets.db.DB_PASSWORD=x")["spec"]["template"]["metadata"]["annotations"]
              for name in MISP_DEPLOYMENTS}
    for name in MISP_DEPLOYMENTS:
        assert env[name]["checksum/env"] != before[name]["checksum/env"], name
        assert secret[name]["checksum/secrets"] != before[name]["checksum/secrets"], name


def test_misp_env_holds_base_env_and_the_env_value_wins():
    base = dict(line.split("=", 1) for line in (chart.CHART / "files/base.env").read_text().splitlines()
                if "=" in line and not line.lstrip().startswith("#"))
    data = env_data("env.MISP_BASEURL=https://misp.example.org")
    assert {k: v for k, v in data.items() if k != "MISP_BASEURL"} == {k: v for k, v in base.items() if k != "MISP_BASEURL"}
    assert data["MISP_BASEURL"] == "https://misp.example.org"


def test_misp_points_at_the_postgres_component_unless_env_says_otherwise():
    data = env_data()
    assert (data["DB_HOST"], data["DB_PORT"]) == ("postgres", "5432")
    assert chart.find("Service", "postgres", "postgres.enabled=true")
    assert env_data("env.DB_HOST=pg.example.org")["DB_HOST"] == "pg.example.org"


def test_the_postgres_pod_refuses_a_claim_from_chart_1(tmp_path):
    """The image looks for older data only where its own layout puts it; chart 1.x kept the cluster in pgdata/."""
    import subprocess
    spec = chart.pod_spec(chart.find("StatefulSet", "postgres", "postgres.enabled=true"))
    postgres = spec["containers"][0]
    assert [m["mountPath"] for m in postgres["volumeMounts"]] == ["/var/lib/postgresql"]
    assert "PGDATA" not in {e["name"] for e in postgres["env"]}
    check = next(c for c in spec["initContainers"] if c["name"] == "check-data")
    script = check["command"][-1].replace("/var/lib/postgresql", str(tmp_path))

    def run():
        return subprocess.run(["sh", "-c", script], env={"PG_MAJOR": "18", "PATH": "/usr/bin:/bin"},
                              capture_output=True, text=True)

    assert run().returncode == 0
    (tmp_path / "18/docker").mkdir(parents=True)
    (tmp_path / "18/docker/PG_VERSION").write_text("18\n")
    assert run().returncode == 0
    (tmp_path / "pgdata").mkdir()
    (tmp_path / "pgdata/PG_VERSION").write_text("17\n")
    refused = run()
    assert refused.returncode == 1
    assert "PostgreSQL 17 data in" in refused.stderr and "runs PostgreSQL 18" in refused.stderr


PG_SECRET = "pg-pguser-misp"
# A user Secret as the Percona and Crunchy operators make it, and a Secret with the Redis password
CONNECTION_SECRETS = (
    f"connectionSecrets[0].name={PG_SECRET}", "connectionSecrets[0].keys.DB_HOST=host",
    "connectionSecrets[0].keys.DB_PORT=port", "connectionSecrets[0].keys.DB_NAME=dbname",
    "connectionSecrets[0].keys.DB_USER=user", "connectionSecrets[0].keys.DB_PASSWORD=password",
    "connectionSecrets[1].name=redis-auth", "connectionSecrets[1].keys.MISP_REDIS_PASSWORD=password")
CONNECTION_REFS = {"DB_HOST": {"name": PG_SECRET, "key": "host"}, "DB_PORT": {"name": PG_SECRET, "key": "port"},
                   "DB_NAME": {"name": PG_SECRET, "key": "dbname"}, "DB_USER": {"name": PG_SECRET, "key": "user"},
                   "DB_PASSWORD": {"name": PG_SECRET, "key": "password"},
                   "MISP_REDIS_PASSWORD": {"name": "redis-auth", "key": "password"}}
# The source each connection var comes from without connectionSecrets
CONNECTION_SOURCES = {"DB_HOST": "misp-env", "DB_PORT": "misp-env", "DB_NAME": "misp-env",
                      "DB_USER": "misp-db", "DB_PASSWORD": "misp-db", "MISP_REDIS_PASSWORD": "misp-app"}
DATABASE_CA = ("databaseCA.name=pg-cluster-cert", "databaseCA.key=ca.crt")


def containers(*sets: str):
    """(where, container, pod spec) of every container, every component on."""
    for doc in chart.objects("migrate.enabled=true", *sets, every_component=True):
        spec = chart.pod_spec(doc)
        for container in (spec or {}).get("initContainers", []) + (spec or {}).get("containers", []):
            yield f"{doc['kind']} {doc['metadata']['name']}: {container['name']}", container, spec


def test_every_pod_takes_the_connection_from_the_connection_secrets():
    """env wins over envFrom: a container gets each var that a source it reads holds, from connectionSecrets."""
    pods = set()
    for where, container, _ in containers(*CONNECTION_SECRETS):
        refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container.get("env", [])
                if "secretKeyRef" in e.get("valueFrom", {})}
        sources = {s.get("name") for e in container.get("envFrom", []) for s in e.values()}
        assert not [r for r in refs.values() if r["name"] in ("misp-db", "misp-app")
                    and r["key"] in CONNECTION_SOURCES], f"{where} reads a connection var of misp-db or misp-app"
        for var, source in CONNECTION_SOURCES.items():
            if source in sources:
                assert refs.get(var) == CONNECTION_REFS[var], f"{where}: {var}"
            elif not where.startswith(("Deployment metrics", "Deployment redis")):
                assert var not in refs, f"{where} takes {var}, which no source of it holds"
        if any(r["name"] == PG_SECRET for r in refs.values()):
            pods.add(where.split(":")[0].split()[1])
    assert {"web", "worker", "metrics", "postgres", "configure-1", "org-sync-1", "housekeeping-logs",
            "periodic-summary"} <= pods
    redis = chart.pod_spec(chart.find("Deployment", "redis", *CONNECTION_SECRETS, every_component=True))
    assert redis["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"] == CONNECTION_REFS["MISP_REDIS_PASSWORD"]


def test_a_connection_secret_fills_only_connection_vars():
    import pytest
    with pytest.raises(RuntimeError, match="ADMIN_KEY"):
        chart.objects("connectionSecrets[0].name=x", "connectionSecrets[0].keys.ADMIN_KEY=key")
    with pytest.raises(RuntimeError, match="missing property 'keys'"):
        chart.objects("connectionSecrets[0].name=x")


def test_the_database_ca_reaches_every_container_that_connects():
    assert "DB_SSLROOTCERT" not in env_data()
    assert env_data(*DATABASE_CA)["DB_SSLROOTCERT"] == "/etc/misp-docker/db-ca/ca.crt"
    seen = set()
    for where, container, spec in containers(*DATABASE_CA):
        reads_db = (any(e.get("secretRef", {}).get("name") == "misp-db" for e in container.get("envFrom", []))
                    or any(e["name"] == "DB_USER" for e in container.get("env", [])))
        if not reads_db:
            continue
        assert {"name": "db-ca", "mountPath": "/etc/misp-docker/db-ca", "readOnly": True} in container["volumeMounts"], where
        assert {"name": "db-ca", "secret": {"secretName": "pg-cluster-cert",
                "items": [{"key": "ca.crt", "path": "ca.crt"}]}} in spec["volumes"], where
        seen.add(where)
    assert len(seen) >= 8


def test_the_policies_reach_an_external_database_and_redis_through_the_egress_values():
    def egress(name: str, *sets: str) -> list:
        return chart.find("NetworkPolicy", name, "networkPolicy.enabled=true", *sets)["spec"]["egress"]

    on = ("postgres.enabled=true", "redis.enabled=true")
    assert {"ports": [{"port": 5432, "protocol": "TCP"}]} in egress("web")
    assert {"ports": [{"port": 6379, "protocol": "TCP"}]} in egress("web")
    assert {"ports": [{"port": 5432, "protocol": "TCP"}]} not in egress("web", *on)
    assert {"ports": [{"port": 7432, "protocol": "TCP"}]} in egress(
        "housekeeping", "networkPolicy.databaseEgress[0].ports[0].port=7432",
        "networkPolicy.databaseEgress[0].ports[0].protocol=TCP")
    names = {d["metadata"]["name"] for d in chart.objects("networkPolicy.enabled=true") if d["kind"] == "NetworkPolicy"}
    assert not names & {"postgres", "cache"}
    assert {"postgres", "cache"} <= {d["metadata"]["name"] for d in chart.objects("networkPolicy.enabled=true", *on)
                                     if d["kind"] == "NetworkPolicy"}


def test_supplied_secrets_render_no_secret():
    assert "Secret" not in kinds("secrets.create=false", "migrate.enabled=true")
    web = chart.find("Deployment", "web", "secrets.create=false")
    assert "checksum/secrets" not in web["spec"]["template"]["metadata"]["annotations"]


def test_the_migration_runs_before_configure_and_is_not_retried():
    job = chart.find("Job", "configure", "migrate.enabled=true")
    spec = chart.pod_spec(job)
    assert [c["name"] for c in spec["initContainers"]] == ["migrate"]
    assert {"secretRef": {"name": "misp-migrate"}} in spec["initContainers"][0]["envFrom"]
    assert job["spec"]["backoffLimit"] == 0
    assert chart.find("Secret", "misp-migrate", "migrate.enabled=true")
    assert "initContainers" not in chart.pod_spec(chart.find("Job", "configure"))


def test_attachments_in_s3_render_no_claim():
    assert "PersistentVolumeClaim" not in kinds("attachments.claim=false")
    volumes = chart.pod_spec(chart.find("Deployment", "web", "attachments.claim=false"))["volumes"]
    assert next(v for v in volumes if v["name"] == "attachments") == {"name": "attachments", "emptyDir": {"sizeLimit": "1Gi"}}


def test_the_helm_test_reaches_web_through_the_network_policy():
    pod = chart.find("Pod", "misp-test", "networkPolicy.enabled=true")
    assert pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    component = pod["metadata"]["labels"]["app.kubernetes.io/component"]
    web = chart.find("NetworkPolicy", "web", "networkPolicy.enabled=true")
    allowed = [peer["podSelector"]["matchLabels"] for rule in web["spec"]["ingress"] for peer in rule["from"]
               if "podSelector" in peer]
    assert {"app.kubernetes.io/component": component} in allowed


def test_every_component_renders_and_the_default_renders_none():
    for component in chart.components():
        assert chart.render_text(*chart.component_sets(component)) != chart.render_text(), (
            f"{component}.enabled=true changes nothing in the render")
    assert not any(d["kind"] in ("StatefulSet", "CronJob", "Ingress", "HTTPRoute", "NetworkPolicy")
                   for d in chart.objects())


def test_a_misspelt_value_is_refused():
    """values.schema.json: a key the chart does not know fails the render instead of doing nothing."""
    import pytest
    with pytest.raises(RuntimeError, match="replica"):
        chart.objects("web.replica=2")


def test_every_volume_mount_names_a_volume_of_its_pod():
    """kubeconform cannot see a mount of a volume the pod lacks; the API server refuses the pod."""
    for sets in ((), ("migrate.enabled=true",), ("attachments.claim=false",)):
        for doc in chart.objects(*sets, every_component=True):
            spec = chart.pod_spec(doc)
            if not spec:
                continue
            volumes = {v["name"] for v in spec.get("volumes", [])}
            # a StatefulSet mounts its claim templates as well
            volumes |= {t["metadata"]["name"] for t in doc["spec"].get("volumeClaimTemplates", [])}
            for container in spec.get("containers", []) + spec.get("initContainers", []):
                mounts = {m["name"] for m in container.get("volumeMounts", [])}
                assert mounts <= volumes, (f"{doc['kind']} {doc['metadata']['name']} ({' '.join(sets) or 'defaults'}): "
                                           f"{container['name']} mounts {sorted(mounts - volumes)}, which the pod lacks")
