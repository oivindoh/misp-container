"""Environment variable handling.

All defaults live in two places:
1. settings.yaml -- for MISP settings (auto-derived env vars)
2. base.env / secrets.env -- for container config (MySQL, PHP, etc.)

apply_defaults() loads settings.yaml defaults into os.environ so that
env("MISP_REDIS_HOST") works everywhere without inline defaults.

The env files (base.env, secrets.env) are loaded by Docker Compose (env_file:)
or Kubernetes (configMapGenerator/secretGenerator) before the container starts,
so those values are already in os.environ.
"""

import os


def env(key, default=None):
    """Get an env var. Returns empty string if not set (unless default given)."""
    return os.environ.get(key, default if default is not None else "")


def apply_defaults():
    """Apply runtime defaults that can't live in env files.

    MISP setting defaults live in settings.yaml (loaded by the config engine).
    Container config defaults live in base.env (loaded by compose/kustomize).
    This function only handles the WORKERS shorthand.
    """
    # Worker queue counts: WORKERS env var as shorthand for all queues
    workers_default = os.environ.get("WORKERS", "5")
    for queue in ("DEFAULT", "PRIO", "EMAIL", "CACHE"):
        key = f"NUM_WORKERS_{queue}"
        if key not in os.environ:
            os.environ[key] = workers_default
    os.environ.setdefault("NUM_WORKERS_UPDATE", "1")
