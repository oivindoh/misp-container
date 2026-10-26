"""Environment variable handling.

The chart sets every env var a pod reads: the misp-env ConfigMap (base.env with
the env value on top), the Secrets, and misp-settings, which carries MISP's
settings (deploy/chart/templates/_settings.tpl).
"""

import os


def env(key, default=None):
    """Get an env var. Returns empty string if not set (unless default given)."""
    return os.environ.get(key, default if default is not None else "")
