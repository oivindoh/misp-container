"""The docs name only what the code has.

Every env var, repository path, mise task, setting, metric, component, task and
exit code a doc names must exist in the tree; where a doc lists all of a kind (the
metrics, the components, the periodic tasks, the upstream checks), the code has no
more of them either. A failure names the doc, the name and where the code keeps it.
"""

import ast
import os
import re
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "files"))
sys.path.insert(0, str(REPO / "scripts"))

from misp_container import env as envmod, task  # noqa: E402
from misp_container.config import derive_env_var  # noqa: E402

DOCS = sorted([REPO / "README.md", REPO / "DEVELOPING.md", REPO / "tests" / "README.md",
               *(REPO / "docs").glob("*.md")])
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
BACKTICKED = re.compile(r"`([^`\n]+)`")

# Upper-case words in backticks that are no env var of this image
NOT_ENV = {
    "BRPOP", "LLEN", "GET", "POST", "JSON", "UTF8", "S256", "SIGKILL", "PDO", "TLS", "API", "CIDR",
    "HTTP_X_FORWARDED_FOR", "REMOTE_ADDR", "NULL", "README", "TODO", "PAR",
    # The host's proxy variables, which kind copies into its node
    "HTTP_PROXY", "HTTPS_PROXY",
}
# The example of DEVELOPING.md, "Adding a setting"
EXAMPLES = {"MISP_MY_SETTING", "MISP.my_setting"}


def rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def repo_files() -> list[Path]:
    found = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        found += [Path(root) / f for f in files]
    return found


def mentions(pattern: re.Pattern) -> list[tuple[Path, str]]:
    """(doc, token) for every backticked token of every doc that matches pattern."""
    return [(doc, m.group(1)) for doc in DOCS for m in BACKTICKED.finditer(doc.read_text())
            if pattern.fullmatch(m.group(1))]


# -- env vars ------------------------------------------------------------------------

def known_env_vars() -> set[str]:
    names = set(envmod.ALIASES) | set(envmod.DB_ALIASES)
    text_files = [p for p in repo_files() if p.suffix in (".py", ".sh", ".env", ".yml", ".yaml", ".toml")
                  or p.name in ("Dockerfile", "Caddyfile") or p.name.endswith(".template")]
    for path in text_files:
        text = path.read_text(errors="replace")
        names |= set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", text))
    names |= {derive_env_var(name) for name in known_settings()}
    return names


ENV_TOKEN = re.compile(r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?:=\S*)?")


def test_every_env_var_a_doc_names_exists():
    known = known_env_vars()
    unknown = sorted({(rel(doc), token.split("=")[0]) for doc, token in mentions(ENV_TOKEN)
                      if token.split("=")[0] not in known | NOT_ENV | EXAMPLES})
    assert not unknown, "docs name env vars that no code, env file, manifest or setting has:\n" + \
        "\n".join(f"  {doc}: {name}" for doc, name in unknown)


# -- settings ------------------------------------------------------------------------

SETTING_TOKEN = re.compile(r"((?:MISP|Plugin|Security|GnuPG|SimpleBackgroundJobs|OidcAuth|LdapAuth|"
                           r"ApacheSecureAuth|Session|Proxy|SMIME|CustomAuth)\.[A-Za-z0-9_]+)(?:=\S*)?")


def known_settings() -> set[str]:
    """Every setting settings.yaml names (in groups) or the catalogue lists (flat)."""
    curated = yaml.safe_load((REPO / "files/misp-config/settings.yaml").read_text())["settings"]
    names = {name for group in curated.values() for name in group}
    names |= set(yaml.safe_load((REPO / "files/misp-config/settings-upstream.yaml").read_text())["settings"])
    # Settings the configure Job sets itself, which the catalogue leaves out
    from update_settings import DYNAMIC
    return names | DYNAMIC


def test_every_setting_a_doc_names_exists():
    known = known_settings()
    unknown = sorted({(rel(doc), token.split("=")[0]) for doc, token in mentions(SETTING_TOKEN)
                      if token.split("=")[0] not in known | EXAMPLES})
    assert not unknown, "docs name settings that neither settings.yaml nor settings-upstream.yaml has:\n" + \
        "\n".join(f"  {doc}: {name}" for doc, name in unknown)


# -- paths and commands --------------------------------------------------------------

PATH_TOKEN = re.compile(r"((?:files|deploy|scripts|tests|docs|\.github)/[\w./-]*)")


def test_every_repository_path_a_doc_names_exists():
    missing = sorted({(rel(doc), token) for doc, token in mentions(PATH_TOKEN)
                      if "*" not in token and not (REPO / token.rstrip("/")).exists()})
    assert not missing, "docs name repository paths that do not exist:\n" + \
        "\n".join(f"  {doc}: {path}" for doc, path in missing)


FILE_TOKEN = re.compile(r"([\w.-]+\.(?:py|sh|yaml|yml|env|toml))")


def test_every_file_name_a_doc_names_exists():
    names = {p.name for p in repo_files()}
    missing = sorted({(rel(doc), token) for doc, token in mentions(FILE_TOKEN) if token not in names})
    assert not missing, "docs name files that the repository does not have:\n" + \
        "\n".join(f"  {doc}: {name}" for doc, name in missing)


def test_every_mise_task_a_doc_names_exists():
    tasks = set(tomllib.loads((REPO / "mise.toml").read_text()).get("tasks", {}))
    missing = sorted({(rel(doc), m.group(1)) for doc in DOCS
                      for m in re.finditer(r"mise run ([\w-]+)", doc.read_text()) if m.group(1) not in tasks})
    assert not missing, "docs name mise tasks that mise.toml does not have:\n" + \
        "\n".join(f"  {doc}: {name}" for doc, name in missing)


# -- lists that must be complete ------------------------------------------------------

def table_names(doc: Path, header: str) -> set[str]:
    """The backticked names in the first column of the table under a header line."""
    lines = doc.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header))
    names = set()
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        names |= set(re.findall(r"`([^`]+)`", line.split("|")[1]))
    return names


def test_the_components_table_lists_every_component():
    listed = table_names(REPO / "docs/kubernetes.md", "| Component |")
    present = {p.name for p in (REPO / "deploy/components").iterdir() if p.is_dir()}
    assert listed == present, (f"docs/kubernetes.md lists {sorted(listed - present)} that deploy/components "
                               f"lacks, and lacks {sorted(present - listed)}")


def test_the_periodic_tasks_table_lists_every_task():
    listed = {name.split()[0] for name in table_names(REPO / "docs/kubernetes.md", "| Task |")}
    assert listed == set(task.TASKS), (f"docs/kubernetes.md lists {sorted(listed - set(task.TASKS))} that "
                                       f"misp_container.task lacks, and lacks {sorted(set(task.TASKS) - listed)}")


def test_the_oidc_table_lists_every_short_name():
    listed = table_names(REPO / "docs/configuration.md", "| Variable | Setting | Default |")
    aliases = {name for name in envmod.ALIASES if name.startswith("OIDC_")}
    # read by the admin step itself, outside the aliases
    aliases.add("OIDC_LOGOUT_URL")
    assert aliases <= listed, f"docs/configuration.md lacks the OIDC short names {sorted(aliases - listed)} (env.py ALIASES)"


def test_the_upstream_checks_table_lists_every_check():
    import check_upstream
    listed = table_names(REPO / "DEVELOPING.md", "| Check | MISP file |")
    checks = {c.name for c in [*check_upstream.CHECKS, check_upstream.PHP_CHECK]}
    assert listed == checks, (f"DEVELOPING.md lists {sorted(listed - checks)} that scripts/check_upstream.py "
                              f"lacks, and lacks {sorted(checks - listed)}")


def exporter_metrics() -> dict[str, str]:
    """{name: type} of every metric the exporter emits."""
    tree = ast.parse((REPO / "files/misp_container/metrics.py").read_text())
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_metric" and len(node.args) >= 3:
            name, _, mtype = node.args[:3]
            if isinstance(name, ast.Constant) and isinstance(mtype, ast.Constant):
                found[name.value] = mtype.value
    return found


def test_the_metrics_doc_lists_every_metric_with_its_type():
    documented = {}
    for line in (REPO / "docs/metrics.md").read_text().splitlines():
        row = re.match(r"\|\s*`(misp_\w+)(?:\{[^}]*\})?`\s*\|\s*(\w+)\s*\|", line)
        if row:
            documented[row.group(1)] = row.group(2)
    emitted = exporter_metrics()
    assert documented == emitted, (
        f"docs/metrics.md and files/misp_container/metrics.py differ: only in the doc "
        f"{sorted(set(documented) - set(emitted))}, only in the code {sorted(set(emitted) - set(documented))}, "
        f"types {sorted((n, documented[n], emitted[n]) for n in documented.keys() & emitted.keys() if documented[n] != emitted[n])}")


def test_the_migration_exit_codes_match_the_job():
    from misp_container import migrate
    codes = {getattr(migrate, n) for n in dir(migrate) if n.startswith("EXIT_")} | {0}
    documented = {int(m) for m in re.findall(r"^\| (\d) \|", (REPO / "docs/migration.md").read_text(), re.M)}
    assert documented == codes, f"docs/migration.md documents exit codes {sorted(documented)}; migrate.py has {sorted(codes)}"


# -- generated regions ---------------------------------------------------------------

def test_the_generated_regions_match_the_code():
    import generate_docs
    stale = [rel(doc) for doc in generate_docs.DOCS if generate_docs.render(doc.read_text()) != doc.read_text()]
    assert not stale, f"generated regions differ from the code in {stale}: run `mise run docs`"


def test_every_generator_fills_a_region():
    import generate_docs
    used = {m.group("name") for doc in generate_docs.DOCS for m in generate_docs.REGION.finditer(doc.read_text())}
    assert used == set(generate_docs.GENERATORS), f"regions {sorted(used)}, generators {sorted(generate_docs.GENERATORS)}"


# -- values written into sentences ---------------------------------------------------

def manifest(path: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((REPO / path).read_text()) if isinstance(d, dict)]


def binary(quantity: str) -> str:
    """256Mi -> 256 MiB, 4Gi -> 4 GiB."""
    return re.sub(r"^(\d+)(Mi|Gi)$", r"\1 \2B", quantity)


def container(path: str, name: str) -> dict:
    spec = manifest(path)[0]["spec"]["template"]["spec"]
    return next(c for c in spec["containers"] if c["name"] == name)


def test_the_architecture_doc_quotes_the_requests_and_limits():
    text = " ".join((REPO / "docs/architecture.md").read_text().split())
    web, worker = container("deploy/base/deployment-web.yaml", "php-fpm"), container("deploy/base/deployment-worker.yaml", "worker")
    modules = container("deploy/base/deployment-modules.yaml", "modules")
    requests = [binary(c["resources"]["requests"]["memory"]) for c in (web, worker, modules)]
    limits = [binary(c["resources"]["limits"]["memory"]) for c in (web, worker, modules)]
    expected = (f"request {requests[0]} for PHP-FPM in a web pod, {requests[1]} for a worker pod and "
                f"{requests[2]} for modules, with limits of {limits[0]}, {limits[1]} and {limits[2]}")
    assert expected in text, f"docs/architecture.md should say: {expected}"


def wave(path: str) -> str:
    return manifest(path)[0]["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]


def test_the_docs_quote_the_sync_waves():
    text = " ".join((REPO / "docs/architecture.md").read_text().split())
    configure, deployments = wave("deploy/base/job-configure.yaml"), wave("deploy/base/deployment-web.yaml")
    org_sync, migrate = wave("deploy/base/job-org-sync.yaml"), wave("deploy/components/migrate/job-migrate.yaml")
    expected = f"`configure` in wave {configure}, the Deployments in wave {deployments}, `org-sync` in wave {org_sync}"
    assert expected in text, f"docs/architecture.md should say: {expected}"
    migration = " ".join((REPO / "docs/migration.md").read_text().split())
    for expected in (f"sync wave {migrate}", f"The Job runs in wave {migrate}"):
        assert expected in migration, f"docs/migration.md should say: {expected}"


def test_the_storage_sizes():
    text = " ".join((REPO / "docs/kubernetes.md").read_text().split())
    claim = manifest("deploy/base/pvc-attachments.yaml")[0]["spec"]
    size, mode = claim["resources"]["requests"]["storage"], claim["accessModes"][0]
    expected = f"PVC `attachments`, {size} {mode}"
    assert expected in text, f"docs/kubernetes.md should say: {expected}"
    for component in ("mariadb", "postgres"):
        statefulset = next(d for d in manifest(f"deploy/components/{component}/{component}.yaml") if d["kind"] == "StatefulSet")
        db = statefulset["spec"]["volumeClaimTemplates"][0]["spec"]
        for expected in (f"StatefulSet with a {db['resources']['requests']['storage']} PVC",
                         f"StatefulSet, {db['resources']['requests']['storage']} {db['accessModes'][0]} PVC"):
            assert expected in text, f"docs/kubernetes.md should say, for {component}: {expected}"


def test_the_task_runner_defaults():
    kube = " ".join((REPO / "docs/kubernetes.md").read_text().split())
    expected = f"`TASK_MAX_QUEUED` jobs (default {task.DEFAULT_MAX_QUEUED})"
    assert expected in kube, f"docs/kubernetes.md should say: {expected}"
    grace = re.search(r'env\("WORKER_STOP_GRACE", "(\d+)"\)', (REPO / "files/entrypoint-worker.py").read_text()).group(1)
    expected = f"`WORKER_STOP_GRACE` seconds (default {grace})"
    assert expected in " ".join((REPO / "docs/architecture.md").read_text().split()), \
        f"docs/architecture.md should say: {expected}"


def test_the_mail_ports_of_the_network_policy():
    policies = manifest("deploy/components/netpol-cilium/networkpolicy.yaml")
    ports = sorted({int(p["port"]) for d in policies for rule in d["spec"].get("egress", [])
                    for to in rule.get("toPorts", []) for p in to.get("ports", []) if int(p["port"]) in (25, 465, 587, 2525)})
    text = " ".join((REPO / "docs/kubernetes.md").read_text().split())
    expected = f"SMTP (ports {', '.join(map(str, ports[:-1]))} and {ports[-1]}"
    assert expected in text, f"docs/kubernetes.md should say: {expected}"


def test_the_logging_doc_names_every_relayed_file():
    from misp_container.logrelay import FILES
    text = (REPO / "docs/configuration.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| Files MISP appends to directly"))
    named = set(re.findall(r"`([\w.-]+\.log)`", row))
    # debug.log and error.log are CakeLog's files, which the logging block drops
    assert named == set(FILES) - {"debug.log", "error.log"}, f"the row names {sorted(named)}; logrelay.FILES has {FILES}"


def test_the_log_format_defaults():
    base = (REPO / "deploy/base/base.env").read_text()
    compose = (REPO / "deploy/compose.env").read_text()
    base_default = re.search(r"^LOG_FORMAT=(\w+)", base, re.M).group(1)
    compose_default = re.search(r"^LOG_FORMAT=(\w+)", compose, re.M).group(1)
    text = " ".join((REPO / "docs/configuration.md").read_text().split())
    for expected in (f"`{base_default}` (the base default", f"`{compose_default}` (the Compose default"):
        assert expected in text, f"docs/configuration.md should say: {expected}"


def test_the_metrics_port():
    port = manifest("deploy/base/service-metrics.yaml")[0]["spec"]["ports"][0]["port"]
    assert f"on port {port}" in " ".join((REPO / "docs/metrics.md").read_text().split()), \
        f"docs/metrics.md should say: on port {port}"

