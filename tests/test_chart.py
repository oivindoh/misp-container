"""The Helm chart renders what the code and the release expect (helm template, no cluster)."""

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "files"))
sys.path.insert(0, str(REPO / "scripts"))

import chart  # noqa: E402
import release  # noqa: E402
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


def test_the_console_tasks_are_the_task_runners():
    helper = (chart.CHART / "templates/_helpers.tpl").read_text()
    listed = set(re.search(r'define "misp.consoleTasks".*?list ((?:"[\w-]+" ?)+)', helper, re.S).group(1).replace('"', "").split())
    assert listed == set(task.CAKE_TASKS)


def test_every_task_has_a_cronjob_but_the_alternatives():
    # workflow runs per workflow ID (cronjobs.workflows); block-invalid-users replaces
    # check-user-validity in userValidity.task
    commands = [chart.pod_spec(d)["containers"][0]["command"] for d in kinds(every_component=True)["CronJob"]]
    scheduled = {c[c.index("misp_container.task") + 1] for c in commands if "misp_container.task" in c}
    assert scheduled == set(task.TASKS) - {task.WORKFLOW_TASK, "block-invalid-users"}


def test_a_console_task_renders_app_config_and_an_api_task_calls_the_api():
    cronjobs = {d["metadata"]["name"]: d for d in kinds(every_component=True)["CronJob"]}
    console, api = cronjobs["periodic-summary"], cronjobs["pull-servers"]
    assert console["metadata"]["labels"]["app.kubernetes.io/component"] == "misp-console-task"
    assert any(m["mountPath"] == "/var/www/MISP/app/Config" for m in chart.pod_spec(console)["containers"][0]["volumeMounts"])
    assert api["metadata"]["labels"]["app.kubernetes.io/component"] == "misp-task"
    assert "envFrom" not in chart.pod_spec(api)["containers"][0]


def test_the_backlog_limit_is_the_task_runners_default():
    values = yaml.safe_load((chart.CHART / "values.yaml").read_text())
    assert values["cronjobs"]["maxQueued"] == task.DEFAULT_MAX_QUEUED


def test_a_workflow_cronjob_passes_its_id():
    cronjob = chart.find("CronJob", "workflow-7", "cronjobs.enabled=true", "cronjobs.workflows.7=0 4 * * *")
    assert chart.pod_spec(cronjob)["containers"][0]["command"][-2:] == ["workflow", "7"]


def test_the_app_version_is_the_image_tag_the_compose_files_default_to():
    meta = yaml.safe_load((chart.CHART / "Chart.yaml").read_text())
    for path in release.COMPOSE_FILES:
        tags = set(re.findall(r"misp-container[a-z-]*:\$\{MISP_IMAGE_TAG:-([^}]+)\}", path.read_text()))
        assert tags <= {meta["appVersion"]}, f"{path.relative_to(REPO)} defaults to {sorted(tags)}, the chart's appVersion is {meta['appVersion']}"


def test_the_chart_version_is_a_release_semver():
    # A pre-release suffix would hide the version from helm upgrade and from semver ranges
    assert re.fullmatch(r"\d+\.\d+\.\d+", yaml.safe_load((chart.CHART / "Chart.yaml").read_text())["version"])


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


def test_the_postgres_component_points_misp_at_it():
    data = env_data("postgres.enabled=true")
    assert (data["DB_ENGINE"], data["DB_HOST"], data["DB_PORT"]) == ("postgres", "postgres", "5432")
    assert env_data("postgres.enabled=true", "env.DB_HOST=pg.example.org")["DB_HOST"] == "pg.example.org"


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
    pod = chart.find("Pod", "misp-test", "ciliumNetworkPolicy.enabled=true")
    assert pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    component = pod["metadata"]["labels"]["app.kubernetes.io/component"]
    web = chart.find("CiliumNetworkPolicy", "web", "ciliumNetworkPolicy.enabled=true")
    allowed = [e["matchLabels"] for rule in web["spec"]["ingress"] for e in rule["fromEndpoints"]]
    assert {"app.kubernetes.io/component": component} in allowed


def test_every_component_renders_and_the_default_renders_none():
    default = {(d["kind"], d["metadata"]["name"]) for d in chart.objects()}
    for component in chart.components():
        added = {(d["kind"], d["metadata"]["name"]) for d in chart.objects(f"{component}.enabled=true")} - default
        assert added, f"{component}.enabled=true renders nothing more"
    assert not any(d["kind"] in ("StatefulSet", "CronJob", "Ingress", "HTTPRoute", "PodDisruptionBudget", "CiliumNetworkPolicy")
                   for d in chart.objects())


def test_the_chart_pins_the_images_the_compose_stack_pins():
    """dependabot bumps the Compose files; values.yaml follows by hand, and this fails until it does."""
    values = yaml.safe_load((chart.CHART / "values.yaml").read_text())
    compose = (REPO / "deploy/docker-compose.yml").read_text()
    for component, service in (("mariadb", "mysql"), ("postgres", "postgres"), ("redis", "redis")):
        image = values[component]["image"]
        assert f"image: {image}\n" in compose, f"{component}.image is {image}, deploy/docker-compose.yml has another for {service}"


def test_a_misspelt_value_is_refused():
    """values.schema.json: a key the chart does not know fails the render instead of doing nothing."""
    import pytest
    with pytest.raises(RuntimeError, match="replica"):
        chart.objects("web.replica=2")
