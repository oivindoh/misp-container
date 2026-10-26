"""Background workers: MISP's queue workers and the log relay, each restarted when it exits.

Runs NUM_WORKERS_<QUEUE> processes of `cake start_worker <queue>` per queue, each in its
own process group. A worker exits on its own when it reaches its maximum run time, and
restarts at once; a process that dies within RAPID_EXIT seconds of its start restarts after
a delay that doubles up to MAX_DELAY. On SIGTERM each process gets SIGTERM, but not the rest of
its group: a worker then takes no new job and exits once its running job, a child in its group,
ends (patches/03). A group that still runs after WORKER_STOP_GRACE seconds gets SIGKILL, and
its running job is lost. Runs as UID 1000 (misp).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import CAKE, MISP_BASE, WORKER_QUEUES, db
from .env import env
from .init import check_writable, prepare
from .log import get as getlog, setup as setup_logging

log = getlog("worker")

PHP_CLI_INI_DIR = "/tmp/php-cli"
RAPID_EXIT = 10
MAX_DELAY = 60
POLL = 0.5


@dataclass
class Program:
    name: str
    command: list[str]
    cwd: str | None = None
    proc: subprocess.Popen | None = field(default=None, repr=False)
    started: float = 0.0
    failures: int = 0
    due: float = 0.0

    def start(self, now: float) -> None:
        self.proc = subprocess.Popen(self.command, cwd=self.cwd, start_new_session=True)
        self.started = now
        log.info("started %s (pid %d)", self.name, self.proc.pid)

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def signal(self, signum: int) -> None:
        try:
            os.kill(self.proc.pid, signum)
        except ProcessLookupError:
            pass

    def signal_group(self, signum: int) -> None:
        try:
            os.killpg(self.proc.pid, signum)
        except ProcessLookupError:
            pass


def programs() -> list[Program]:
    """One program per worker process of each queue, then the log relay."""
    out = [Program(f"{queue}_{n:02d}", [CAKE, "start_worker", queue], cwd=MISP_BASE)
           for queue in WORKER_QUEUES for n in range(int(env(f"NUM_WORKERS_{queue.upper()}", "0")))]
    out.append(Program("logrelay", [sys.executable, "-m", "misp_container.logrelay"]))
    return out


class Supervisor:
    def __init__(self, programs: list[Program], grace: float):
        self.programs = programs
        self.grace = grace
        self.stopping = False

    def stop(self, _signum=None, _frame=None) -> None:
        self.stopping = True

    def reap(self, now: float) -> None:
        """Schedule the restart of each program that exited."""
        for p in self.programs:
            if p.proc is None or p.proc.poll() is None:
                continue
            p.failures = p.failures + 1 if now - p.started < RAPID_EXIT else 0
            delay = min(2 ** (p.failures - 1), MAX_DELAY) if p.failures else 0
            log.log(logging.WARNING if p.failures else logging.INFO, "%s exited with %s after %ds; restarting in %ds",
                    p.name, p.proc.returncode, int(now - p.started), delay)
            p.proc = None
            p.due = now + delay

    def start_due(self, now: float) -> None:
        for p in self.programs:
            if p.proc is None and now >= p.due:
                p.start(now)

    def shutdown(self) -> int:
        running = [p for p in self.programs if p.running()]
        log.info("stopping %d processes; a running job gets %ds to finish", len(running), self.grace)
        for p in running:
            p.signal(signal.SIGTERM)
        deadline = time.monotonic() + self.grace
        while any(p.running() for p in running) and time.monotonic() < deadline:
            time.sleep(POLL)
        for p in running:
            if p.running():
                log.warning("%s still runs after %ds; killing it", p.name, self.grace)
                p.signal_group(signal.SIGKILL)
                p.proc.wait()
        return 0

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while not self.stopping:
            now = time.monotonic()
            self.reap(now)
            self.start_due(now)
            time.sleep(POLL)
        return self.shutdown()


def limit_worker_memory() -> None:
    """A memory limit per worker process, in place of the CLI's unlimited default.

    Debian's CLI php.ini sets memory_limit -1, so one runaway job would grow until
    the kubelet kills the pod with every worker in it. With a limit the job fails
    alone. PHP appends the directory to its scan path when the value starts with
    a colon; the workers inherit the variable.
    """
    limit = env("WORKER_MEMORY_LIMIT") or env("PHP_MEMORY_LIMIT", "2048M")
    Path(PHP_CLI_INI_DIR).mkdir(parents=True, exist_ok=True)
    Path(PHP_CLI_INI_DIR, "memory.ini").write_text(f"memory_limit = {limit}\n")
    os.environ["PHP_INI_SCAN_DIR"] = f":{PHP_CLI_INI_DIR}"
    log.info("worker memory limit %s per process", limit)


def main() -> None:
    setup_logging("worker")
    log.info("MISP worker container starting")
    prepare()
    if not env("PLUGIN_S3_BUCKET_NAME"):
        check_writable(env("MISP_ATTACHMENTS_DIR"), "attachments")
    db.wait_for_db(retries=60, wait_seconds=5)
    db.wait_for_configure()
    limit_worker_memory()
    supervisor = Supervisor(programs(), float(env("WORKER_STOP_GRACE", "300")))
    log.info("starting %d worker processes on the queues %s", len(supervisor.programs) - 1, ", ".join(WORKER_QUEUES))
    sys.exit(supervisor.run())


if __name__ == "__main__":
    main()
