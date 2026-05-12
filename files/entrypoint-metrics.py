#!/usr/bin/env python3
"""MISP Prometheus metrics exporter.

Lightweight HTTP server that exposes /metrics in Prometheus exposition format.
Queries the MISP database directly (no API dependency) and checks remote
server reachability.

Default port: 9191
"""

import socket
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

from misp_container.env import apply_defaults
from misp_container.db import wait_for_mysql
from misp_container.metrics import collect_all
from misp_container.log import setup as setup_logging, get as getlog

setup_logging("metrics")
log = getlog("metrics")

PORT = 9191


class MetricsHandler(BaseHTTPRequestHandler):
    """Handle /metrics, /healthz, /ready endpoints."""

    def do_GET(self):
        if self.path == "/metrics":
            body = collect_all().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path in ("/healthz", "/ready"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found\n")

    def log_message(self, format, *args):
        """Suppress per-request access logs (noisy with Prometheus scraping)."""
        pass


def main():
    log.info("MISP metrics exporter starting")
    apply_defaults()
    wait_for_mysql(retries=30, wait_seconds=5)

    # Try IPv6 dual-stack first (K8s), fall back to IPv4 (Docker)
    try:
        HTTPServer.address_family = socket.AF_INET6
        server = HTTPServer(("::", PORT), MetricsHandler)
    except OSError:
        HTTPServer.address_family = socket.AF_INET
        server = HTTPServer(("0.0.0.0", PORT), MetricsHandler)
    log.info("serving metrics on :%d/metrics", PORT)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
