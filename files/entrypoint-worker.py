#!/usr/bin/env python3
"""Background worker entrypoint: runs MISP's queue workers (misp_container.worker)."""

from misp_container.worker import main

main()
