"""Periodic MISP tasks.

Usage: python3 -m misp_container.task <task> [workflow id]

Runs as a Kubernetes CronJob (the chart's cronjobs component) or on demand from
Compose. API tasks make one or a few API calls, and MISP queues the real work
as background jobs for the workers. Console tasks have no API: the pod renders
app/Config and runs MISP's console, as the configure Job does.
"""

import subprocess
import sys

from .api import MISPClient, APIError
from .env import env
from .log import is_log_line, setup as setup_logging, get as getlog

log = getlog("task")

# Simple tasks: one POST to an endpoint
ENDPOINTS = {
    "cache-feeds": "/feeds/cacheFeeds/all",
    "fetch-feeds": "/feeds/fetchFromAllFeeds",
    "cache-servers": "/servers/cache/all",
    "update-galaxies": "/galaxies/update",
    "update-taxonomies": "/taxonomies/update",
    "update-warninglists": "/warninglists/update",
    "update-noticelists": "/noticelists/update",
    "update-object-templates": "/objectTemplates/update",
}

# Server tasks: one POST per sync server that has the flag enabled
# (MISP answers GET on these actions with 405)
SERVER_TASKS = {
    "pull-servers": ("pull", "/servers/pull/{id}"),
    "push-servers": ("push", "/servers/push/{id}"),
}

# Index tasks: list the items first, then act on them
INDEX_TASKS = ("push-taxii", "sharing-group-blueprints")

# Ad-hoc workflow by ID: python3 -m misp_container.task workflow <id>
WORKFLOW_TASK = "workflow"

# Console tasks: (shell, command) for app/Console/cake
CAKE_TASKS = {
    "periodic-summary": ("Server", "sendPeriodicSummaryToUsers"),
    "check-user-validity": ("Admin", "checkUserValidity"),
    "block-invalid-users": ("Admin", "blockInvalidUsers"),
}

API_TASKS = tuple(ENDPOINTS) + tuple(SERVER_TASKS) + INDEX_TASKS + (WORKFLOW_TASK,)
TASKS = API_TASKS + tuple(CAKE_TASKS)

# What each task does, for the usage message and docs/kubernetes.md (scripts/generate_docs.py)
DESCRIPTIONS = {
    "cache-feeds": "Cache every feed",
    "fetch-feeds": "Fetch every enabled feed",
    "cache-servers": "Cache the events of every server",
    "update-galaxies": "Update MISP's bundled galaxies",
    "update-taxonomies": "Update MISP's bundled taxonomies",
    "update-warninglists": "Update MISP's bundled warninglists",
    "update-noticelists": "Update MISP's bundled noticelists",
    "update-object-templates": "Update MISP's bundled object templates",
    "pull-servers": "Pull from every server with pull enabled",
    "push-servers": "Push to every server with push enabled",
    "push-taxii": "Push to every enabled TAXII server",
    "sharing-group-blueprints": "Apply the sharing group blueprints",
    WORKFLOW_TASK: "Run one ad-hoc workflow, by its ID",
    "periodic-summary": "Send the daily, weekly (Mondays) and monthly (the first) summaries users subscribed to",
    "check-user-validity": "Report every account as valid or invalid at the OIDC or LDAP provider",
    "block-invalid-users": "Disable the accounts the OIDC or LDAP provider no longer backs",
}

# Every task type and action that MISP's scheduler offers
# (app/Console/Command/SchedulerWorkerShell.php), with the task that does the
# same work. scripts/check_scheduler_coverage.py fails a MISP release whose
# scheduler offers one that is missing here.
SCHEDULER_COVERAGE = {
    ("Server", "pull"): "pull-servers",
    ("Server", "push"): "push-servers",
    ("Server", "cache"): "cache-servers",
    ("Feed", "fetch"): "fetch-feeds",
    ("Feed", "cache"): "cache-feeds",
    ("TAXII", "push"): "push-taxii",
    ("Workflow", ""): WORKFLOW_TASK,
    ("Periodic Summary", "send"): "periodic-summary",
    ("Admin", "updateGalaxies"): "update-galaxies",
    ("Admin", "updateTaxonomies"): "update-taxonomies",
    ("Admin", "updateWarningLists"): "update-warninglists",
    ("Admin", "updateNoticeLists"): "update-noticelists",
    ("Admin", "updateObjectTemplates"): "update-object-templates",
    ("Admin", "checkUserValidity"): "check-user-validity",
    ("Admin", "blockInvalidUsers"): "block-invalid-users",
}

# MISP answers these calls after queueing the job; feed and server calls can
# still take a while on a busy instance.
REQUEST_TIMEOUT = 600

# Backlog protection: with this many jobs queued or running (misp_jobs_queued
# from the metrics exporter), a run dispatches nothing and exits 0. An
# unreachable exporter does not block dispatch.
DEFAULT_MAX_QUEUED = 200
DEFAULT_METRICS_URL = "http://metrics:9191/metrics"


def queued_jobs(metrics_url: str, timeout: int = 5):
    """Sum of misp_jobs_queued from the exporter, or None when it cannot be read."""
    import urllib.request
    try:
        with urllib.request.urlopen(metrics_url, timeout=timeout) as resp:
            text = resp.read().decode()
    except Exception as e:
        log.warning("cannot read %s (%s); dispatching without the backlog check", metrics_url, e)
        return None
    total = 0
    seen = False
    for line in text.splitlines():
        if line.startswith("misp_jobs_queued"):
            try:
                total += int(float(line.rsplit(" ", 1)[1]))
                seen = True
            except ValueError:
                pass
    return total if seen else None


def _items(response, key: str) -> list[dict]:
    """The records of a MISP index, whether wrapped as {"Key": {...}} or not."""
    items = []
    for item in response if isinstance(response, list) else []:
        inner = item.get(key, item) if isinstance(item, dict) else None
        if isinstance(inner, dict):
            items.append(inner)
    return items


def _enabled(value) -> bool:
    return value in (True, 1, "1", "true")


def run_task(client, name: str, arg: str = "") -> dict:
    """Run one API task. Returns {"calls": n, "errors": [...]}."""
    calls = 0
    errors = []

    def post(path, label=""):
        nonlocal calls
        log.info("POST %s%s", path, f" ({label})" if label else "")
        try:
            result = client.post(path, {})
            calls += 1
            return result
        except APIError as e:
            errors.append(str(e))
            return None

    if name in ENDPOINTS:
        post(ENDPOINTS[name])
    elif name in SERVER_TASKS:
        flag, template = SERVER_TASKS[name]
        for server in client.get_servers().values():
            if server.get(flag):
                post(template.format(id=server["id"]), label=server.get("name", ""))
        log.info("%s: %d server(s)", name, calls)
    elif name == "push-taxii":
        for server in _items(client.get("/taxiiServers/index"), "TaxiiServer"):
            if _enabled(server.get("enabled")):
                post(f"/taxiiServers/push/{server['id']}", label=server.get("name", ""))
        log.info("%s: %d TAXII server(s)", name, calls)
    elif name == "sharing-group-blueprints":
        blueprints = _items(client.get("/sharingGroupBlueprints/index"), "SharingGroupBlueprint")
        # MISP answers an execute without blueprints with 404
        if blueprints:
            post("/sharingGroupBlueprints/execute")
        log.info("%s: %d blueprint(s)", name, len(blueprints))
    elif name == WORKFLOW_TASK:
        if not arg.isdigit():
            raise ValueError("the workflow task needs a numeric workflow ID")
        result = post(f"/workflows/executeWorkflow/{arg}")
        if isinstance(result, dict) and result.get("success") is False:
            calls -= 1
            errors.append(f"workflow {arg}: {result.get('outcome', 'failed')}")
    else:
        raise ValueError(f"unknown task {name!r}; choose one of {', '.join(TASKS)}")

    for err in errors:
        log.error("%s", err)
    return {"calls": calls, "errors": errors}


def run_cake_task(name: str) -> int:
    """Run one console task in this pod. Returns the console's exit code."""
    from . import CAKE
    from .env import apply_defaults
    from .init import prepare

    apply_defaults()
    prepare()
    args = CAKE_TASKS[name]
    log.info("cake %s", " ".join(args))
    result = subprocess.run([CAKE, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in result.stdout.splitlines():
        # MISP's own log lines are in the container format already; the console's
        # plain output gets it here
        if is_log_line(line):
            print(line, flush=True)
        elif line.strip():
            log.info("%s", line)
    if result.returncode != 0:
        log.error("cake %s exited %d", " ".join(args), result.returncode)
    return result.returncode


def main(argv: list[str]) -> None:
    setup_logging("task")
    name = argv[0] if argv else ""
    arg_count = 2 if name == WORKFLOW_TASK else 1
    if name not in TASKS or len(argv) != arg_count:
        log.error("usage: python3 -m misp_container.task <task> (workflow takes a workflow ID)")
        for known in TASKS:
            log.error("  %-24s %s", known, DESCRIPTIONS[known])
        sys.exit(2)

    if name in CAKE_TASKS:
        sys.exit(1 if run_cake_task(name) else 0)

    api_key = env("ADMIN_KEY")
    if not api_key:
        log.error("ADMIN_KEY is not set; the task runner needs an admin API key")
        sys.exit(1)
    base_url = env("SYNC_BASE_URL", env("MISP_BASEURL"))

    max_queued = int(env("TASK_MAX_QUEUED", str(DEFAULT_MAX_QUEUED)))
    if max_queued > 0:
        queued = queued_jobs(env("TASK_METRICS_URL", DEFAULT_METRICS_URL))
        if queued is not None and queued >= max_queued:
            log.warning("%d jobs queued or running (limit %d); %s dispatches nothing this run",
                        queued, max_queued, name)
            sys.exit(0)

    client = MISPClient(base_url, api_key)
    client.timeout = REQUEST_TIMEOUT
    result = run_task(client, name, argv[1] if len(argv) > 1 else "")
    # A partner that refuses one pull is that partner's problem (the metrics
    # exporter reports it); the run only fails when no call succeeded.
    sys.exit(1 if result["errors"] and not result["calls"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
