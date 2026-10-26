"""Periodic MISP tasks.

Usage: python3 -m misp_container.task <task> [workflow id]

Runs as a Kubernetes CronJob (the chart's cronjobs component), or as a Job made
from one. Every task runs MISP's console in this pod, with the command line MISP's
own scheduler would hand a worker (app/Console/Command/SchedulerWorkerShell.php):
the pod renders app/Config, and the work happens here, not on the workers. The
server tasks run once per server, one after the other, so a CronJob that forbids
overlapping runs also keeps two pulls of one server apart.
"""

import subprocess
import sys

from . import CAKE, db
from .env import apply_defaults
from .init import prepare
from .log import is_log_line, setup as setup_logging, get as getlog
from .worker import limit_worker_memory

log = getlog("task")

# The user the console acts as: user 1, the admin the configure step keeps
TASK_USER = "1"
# Stands for the workflow ID, or the server ID, in a command line
ARG = "<id>"

# Ad-hoc workflow by ID: python3 -m misp_container.task workflow <id>
WORKFLOW_TASK = "workflow"

# The console command line of each task, for app/Console/cake
TASKS = {
    "cache-feeds": ("Server", "cacheFeed", TASK_USER, "all"),
    "fetch-feeds": ("Server", "fetchFeed", TASK_USER, "all"),
    "cache-servers": ("Server", "cacheServer", TASK_USER, ARG),
    "update-galaxies": ("Admin", "updateGalaxies", "false"),
    "update-taxonomies": ("Admin", "updateTaxonomies"),
    "update-warninglists": ("Admin", "updateWarningLists"),
    "update-noticelists": ("Admin", "updateNoticeLists"),
    "update-object-templates": ("Admin", "updateObjectTemplates", TASK_USER),
    "pull-servers": ("Server", "pull", TASK_USER, ARG, "full"),
    "push-servers": ("Server", "push", TASK_USER, ARG, "full"),
    "push-taxii": ("Server", "push_taxii", TASK_USER, "all"),
    "sharing-group-blueprints": ("Admin", "executeSGBlueprint", "all"),
    WORKFLOW_TASK: ("Workflow", "executeAdHocWorkflow", ARG),
    "periodic-summary": ("Server", "sendPeriodicSummaryToUsers"),
    "check-user-validity": ("Admin", "checkUserValidity"),
    "block-invalid-users": ("Admin", "blockInvalidUsers"),
}

# The server tasks: each runs for every server whose column is true, in MISP's priority order
SERVER_TASKS = {"pull-servers": "pull", "push-servers": "push", "cache-servers": "caching_enabled"}
# The start of a line in which pull and push report a failed server; both exit 0 anyway
FAILED = ("ERROR:", "Job failed.")

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
# same work. The scheduler-tasks check of scripts/check_upstream.py fails a MISP
# release whose scheduler offers one that is missing here.
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


def command(name: str, arg: str = "") -> list[str]:
    """The cake command line of a task."""
    return [CAKE, *(arg if part == ARG else part for part in TASKS[name])]


def servers(name: str) -> list[int]:
    """The IDs of the servers a server task runs for."""
    column = SERVER_TASKS[name]
    return [row["id"] for row in db.dict_query(f"SELECT id FROM servers WHERE {column} ORDER BY priority, id")]


def console(args: list[str]) -> bool:
    """Run a console command, its output streamed as it comes; True when it succeeded."""
    log.info("cake %s", " ".join(args[1:]))
    failed = False
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as proc:
        for line in proc.stdout:
            line = line.rstrip("\n")
            failed = failed or line.startswith(FAILED)
            # MISP's own log lines are in the container format already; the console's
            # plain output gets it here
            if is_log_line(line):
                print(line, flush=True)
            elif line.strip():
                log.info("%s", line)
    if proc.returncode != 0:
        log.error("cake %s exited %d", " ".join(args[1:]), proc.returncode)
    elif failed:
        log.error("cake %s reported a failure", " ".join(args[1:]))
    return proc.returncode == 0 and not failed


def run(name: str, arg: str = "") -> int:
    """Run one task's console command in this pod: 0 when it succeeded, else 1."""
    apply_defaults()
    prepare()
    limit_worker_memory()
    db.wait_for_db(retries=60, wait_seconds=5)
    db.wait_for_configure()
    if name not in SERVER_TASKS:
        return 0 if console(command(name, arg)) else 1
    ids = servers(name)
    if not ids:
        log.info("%s: no server has %s on, nothing to run", name, SERVER_TASKS[name])
    results = [console(command(name, str(server))) for server in ids]
    return 0 if all(results) else 1


def main(argv: list[str]) -> None:
    setup_logging("task")
    name = argv[0] if argv else ""
    arg_count = 2 if name == WORKFLOW_TASK else 1
    if name not in TASKS or len(argv) != arg_count or (name == WORKFLOW_TASK and not argv[1].isdigit()):
        log.error("usage: python3 -m misp_container.task <task> (workflow takes a numeric workflow ID)")
        for known in TASKS:
            log.error("  %-24s %s", known, DESCRIPTIONS[known])
        sys.exit(2)
    sys.exit(run(name, argv[1] if len(argv) > 1 else ""))


if __name__ == "__main__":
    main(sys.argv[1:])
