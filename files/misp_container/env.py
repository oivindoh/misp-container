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


# Settings that inherit from a primary env var unless set explicitly.
# Users set MISP_BASEURL, ADMIN_EMAIL, MISP_REDIS_* and the module URL once.
DERIVED = {
    "MISP_BASEURL": ("MISP_EXTERNAL_BASEURL", "SECURITY_REST_CLIENT_BASEURL"),
    "MISP_REDIS_HOST": ("SIMPLEBACKGROUNDJOBS_REDIS_HOST", "PLUGIN_ZEROMQ_REDIS_HOST"),
    "MISP_REDIS_PORT": ("SIMPLEBACKGROUNDJOBS_REDIS_PORT", "PLUGIN_ZEROMQ_REDIS_PORT"),
    "MISP_REDIS_PASSWORD": ("SIMPLEBACKGROUNDJOBS_REDIS_PASSWORD", "PLUGIN_ZEROMQ_REDIS_PASSWORD"),
    "PLUGIN_ENRICHMENT_SERVICES_URL": ("PLUGIN_IMPORT_SERVICES_URL", "PLUGIN_EXPORT_SERVICES_URL",
                                       "PLUGIN_ACTION_SERVICES_URL"),
}


def apply_defaults():
    """Apply runtime defaults that can't live in env files.

    MISP setting defaults live in settings.yaml (loaded by the config engine).
    Container config defaults live in base.env (loaded by compose/kustomize).
    This function handles the WORKERS shorthand and the derived variables.
    """
    # Worker queue counts: WORKERS env var as shorthand for all queues
    workers_default = os.environ.get("WORKERS", "5")
    for queue in ("DEFAULT", "PRIO", "EMAIL", "CACHE"):
        key = f"NUM_WORKERS_{queue}"
        if key not in os.environ:
            os.environ[key] = workers_default
    os.environ.setdefault("NUM_WORKERS_UPDATE", "1")

    for source, targets in DERIVED.items():
        value = os.environ.get(source, "")
        if value:
            for target in targets:
                os.environ.setdefault(target, value)
    misp_email = os.environ.get("MISP_EMAIL") or os.environ.get("ADMIN_EMAIL", "")
    if misp_email:
        os.environ.setdefault("MISP_CONTACT", misp_email)
        os.environ.setdefault("GNUPG_EMAIL", misp_email)
