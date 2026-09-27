"""Periodic MISP tasks through the REST API.

Usage: python3 -m misp_container.task <task>

Runs as a Kubernetes CronJob (deploy/components/cronjobs) or on demand from
Compose. Each task is one or a few API calls; MISP queues the real work as
background jobs for the workers.
"""

import sys

from .api import MISPClient, APIError
from .env import env
from .log import setup as setup_logging, get as getlog

log = getlog("task")

# Simple tasks: one POST to an endpoint
ENDPOINTS = {
    "cache-feeds": "/feeds/cacheFeeds/all",
    "fetch-feeds": "/feeds/fetchFromAllFeeds",
    "update-galaxies": "/galaxies/update",
    "update-taxonomies": "/taxonomies/update",
    "update-warninglists": "/warninglists/update",
    "update-noticelists": "/noticelists/update",
}

# Server tasks: one GET per sync server that has the flag enabled
SERVER_TASKS = {
    "pull-servers": ("pull", "/servers/pull/{id}"),
    "push-servers": ("push", "/servers/push/{id}"),
}

TASKS = tuple(ENDPOINTS) + tuple(SERVER_TASKS)

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


def run_task(client, name: str) -> dict:
    """Run one task. Returns {"calls": n, "errors": [...]}."""
    calls = 0
    errors = []

    if name in ENDPOINTS:
        path = ENDPOINTS[name]
        log.info("POST %s", path)
        try:
            client.post(path, {})
            calls += 1
        except APIError as e:
            errors.append(str(e))
    elif name in SERVER_TASKS:
        flag, template = SERVER_TASKS[name]
        for server in client.get_servers().values():
            if not server.get(flag):
                continue
            path = template.format(id=server["id"])
            log.info("GET %s (%s)", path, server.get("name", ""))
            try:
                client.get(path)
                calls += 1
            except APIError as e:
                errors.append(str(e))
        log.info("%s: %d server(s)", name, calls)
    else:
        raise ValueError(f"unknown task {name!r}; choose one of {', '.join(TASKS)}")

    for err in errors:
        log.error("%s", err)
    return {"calls": calls, "errors": errors}


def main(argv: list[str]) -> None:
    setup_logging("task")
    if len(argv) != 1 or argv[0] not in TASKS:
        log.error("usage: python3 -m misp_container.task <%s>", "|".join(TASKS))
        sys.exit(2)
    name = argv[0]

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
    result = run_task(client, name)
    # A partner that refuses one pull is that partner's problem (the metrics
    # exporter reports it); the run only fails when no call succeeded.
    sys.exit(1 if result["errors"] and not result["calls"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
