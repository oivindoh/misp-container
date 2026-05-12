#!/usr/bin/env python3
"""Background worker entrypoint.

Waits for web container to finish configuring (MISP.live=true),
generates supervisord config, then exec's supervisord.
Runs as UID 1000 (misp) - no root operations.
"""

import os
import sys
import time
from pathlib import Path

from misp_container import CAKE, MISP_BASE
from misp_container.env import apply_defaults, env
from misp_container import db
from misp_container.log import setup as setup_logging, get as getlog

SUPERVISORD_CONF = "/tmp/supervisord-workers.conf"

WORKER_TEMPLATE = """
[program:{name}]
directory={misp_base}
command={cake} start_worker {name}
process_name=%(program_name)s_%(process_num)02d
numprocs={numprocs}
autostart=true
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
"""

SCHEDULER_TEMPLATE = """
[program:scheduler]
directory={misp_base}
command={cake} scheduler_worker
process_name=%(program_name)s
numprocs=1
autostart=true
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
"""


def generate_supervisord_config():
    """Generate supervisord config for worker queues."""
    log.info("generating supervisord configuration")

    sv_user = env("SIMPLEBACKGROUNDJOBS_SUPERVISOR_USER")
    sv_pass = env("SIMPLEBACKGROUNDJOBS_SUPERVISOR_PASSWORD")

    sv_port = env("SIMPLEBACKGROUNDJOBS_SUPERVISOR_PORT")

    header = f"""[supervisord]
nodaemon=true
logfile=/dev/null
logfile_maxbytes=0
pidfile=/tmp/supervisord.pid

[unix_http_server]
file=/tmp/supervisor.sock
chmod=0700
username={sv_user}
password={sv_pass}

# inet_http_server is NOT used -- supervisord only supports IPv4.
# A Python TCP proxy handles the network listener (IPv4+IPv6) and
# forwards to the unix socket. See _tcp_proxy below.

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///tmp/supervisor.sock
username={sv_user}
password={sv_pass}
"""

    sections = [header]
    for name in ("default", "prio", "email", "update", "cache"):
        numprocs = env(f"NUM_WORKERS_{name.upper()}", "0")
        if int(numprocs) > 0:
            sections.append(WORKER_TEMPLATE.format(
                name=name, misp_base=MISP_BASE, cake=CAKE, numprocs=numprocs,
            ))

    if env("ENABLE_SCHEDULER") == "true":
        sections.append(SCHEDULER_TEMPLATE.format(misp_base=MISP_BASE, cake=CAKE))
    else:
        log.info("scheduler disabled (ENABLE_SCHEDULER=false)")

    # MISP's BackgroundJobsTool filters processes by group name 'misp-workers'.
    # Group all worker programs under this name so the diagnostic page sees them.
    worker_programs = [name for name in ("default", "prio", "email", "update", "cache")
                       if int(env(f"NUM_WORKERS_{name.upper()}", "0")) > 0]
    if env("ENABLE_SCHEDULER") == "true":
        worker_programs.append("scheduler")
    if worker_programs:
        sections.append(f"\n[group:misp-workers]\nprograms={','.join(worker_programs)}\n")

    Path(SUPERVISORD_CONF).write_text("".join(sections))


def wait_for_web():
    """Wait for the web container to finish configuring (MISP.live=true)."""
    log.info("waiting for web container to finish configuring")
    db.wait_for_mysql(retries=60, wait_seconds=5)

    for i in range(120, 0, -1):
        try:
            result = db.query(
                "SELECT value FROM system_settings WHERE setting='MISP.live';"
            ).strip().strip('"')
            if result == "true":
                log.info("MISP is live, web configuration complete")
                return
        except Exception:
            pass
        log.info("waiting for MISP.live=true (%d retries left)", i)
        time.sleep(3)

    log.error("web container did not set MISP.live=true after timeout")
    sys.exit(1)


# -- Main --

setup_logging("worker")
log = getlog("worker")

log.info("MISP worker container starting")

apply_defaults()
wait_for_web()
generate_supervisord_config()

log.info("starting background workers via supervisord")

# Supervisord only supports AF_INET and AF_UNIX -- no IPv6.
# We disable inet_http_server and instead run a small TCP proxy
# that listens on all protocols (IPv4 + IPv6) and forwards to the
# unix socket. Works on single-stack IPv4, single-stack IPv6, and dual-stack.
import socket
import threading

SUPERVISOR_SOCK = "/tmp/supervisor.sock"

def _tcp_proxy(port):
    """Listen on TCP (dual-stack) and forward to supervisord's unix socket."""
    def relay(src, dst):
        try:
            while True:
                data = src.recv(4096)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            src.close()
            dst.close()

    def serve(srv):
        while True:
            try:
                client, _ = srv.accept()
                backend = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                backend.connect(SUPERVISOR_SOCK)
                threading.Thread(target=relay, args=(client, backend), daemon=True).start()
                threading.Thread(target=relay, args=(backend, client), daemon=True).start()
            except OSError:
                pass

    # Try IPv6 dual-stack first (accepts IPv4 via mapped addresses on Linux)
    for family, addr in [(socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")]:
        try:
            srv = socket.socket(family, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                # Allow IPv4 connections on the same socket (dual-stack)
                srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            srv.bind((addr, port))
            srv.listen(16)
            threading.Thread(target=serve, args=(srv,), daemon=True).start()
            log.info("supervisor TCP proxy listening on %s:%d", addr, port)
            return
        except OSError:
            continue
    log.warning("could not start supervisor TCP proxy on port %d", port)

sv_port_num = int(env("SIMPLEBACKGROUNDJOBS_SUPERVISOR_PORT"))
_tcp_proxy(sv_port_num)

# Can't use execvp (kills threads). Run supervisord as a subprocess and
# forward signals so tini can still manage the process tree.
import signal
import subprocess

proc = subprocess.Popen(
    ["/usr/local/bin/supervisord", "-c", SUPERVISORD_CONF],
)

def _forward_signal(signum, _frame):
    proc.send_signal(signum)

signal.signal(signal.SIGTERM, _forward_signal)
signal.signal(signal.SIGINT, _forward_signal)

sys.exit(proc.wait())
