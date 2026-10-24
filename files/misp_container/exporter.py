"""The metrics exporter's HTTP server: /metrics, and /healthz and /ready for the probes.

Its own paths stay out of misp_container/metrics.py, whose path strings the
upstream guard reads as MISP API routes.
"""

import socket
from http.server import BaseHTTPRequestHandler, HTTPServer

from .log import get as getlog

log = getlog("metrics")

PORT = 9191


def serve(collect, port: int = PORT) -> None:
    """Serve collect()'s text on /metrics until interrupted."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/metrics":
                self.answer(200, collect(), "text/plain; version=0.0.4; charset=utf-8")
            elif self.path in ("/healthz", "/ready"):
                self.answer(200, "ok\n", "text/plain")
            else:
                self.answer(404, "not found\n", "text/plain")

        def answer(self, status, text, content_type):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            """No access log: Prometheus scrapes too often for it to help."""

    # Dual-stack where the host offers IPv6 (Kubernetes), IPv4 otherwise
    try:
        HTTPServer.address_family = socket.AF_INET6
        server = HTTPServer(("::", port), Handler)
    except OSError:
        HTTPServer.address_family = socket.AF_INET
        server = HTTPServer(("0.0.0.0", port), Handler)
    log.info("serving metrics on :%d/metrics", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()
