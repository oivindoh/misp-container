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

    client = MISPClient(base_url, api_key)
    client.timeout = REQUEST_TIMEOUT
    result = run_task(client, name)
    # A partner that refuses one pull is that partner's problem (the metrics
    # exporter reports it); the run only fails when no call succeeded.
    sys.exit(1 if result["errors"] and not result["calls"] else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
