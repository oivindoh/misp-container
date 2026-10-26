"""Thin wrapper around the CakePHP cake CLI."""

import subprocess

from . import CAKE
from .log import get as getlog

log = getlog("cake")


def run(*args):
    """Run a cake command. Returns (returncode, stdout)."""
    result = subprocess.run([CAKE, *args], capture_output=True, text=True)
    return result.returncode, result.stdout.strip()


def run_checked(*args):
    """Run a cake command that the configure step cannot do without; raise when it fails."""
    result = subprocess.run([CAKE, *args], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        raise RuntimeError(f"cake {' '.join(args)} exited {result.returncode}: " + " / ".join(detail))


def set_setting(setting, value, force=False):
    """Set a MISP setting via cake Admin setSetting. Logs and returns False on failure."""
    args = ["Admin", "setSetting", "-q"]
    if force:
        args.append("-f")
    args.extend([setting, str(value)])
    result = subprocess.run([CAKE, *args], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        log.warning("cake setSetting %s failed (rc=%d): %s", setting, result.returncode,
                    detail[-1] if detail else "no output")
        return False
    return True


def run_updates():
    """Run MISP database updates."""
    log.info("running MISP database updates")
    run_checked("Admin", "runUpdates")


def user_init():
    """Initialize default user/role/org."""
    run_checked("user", "init")


def user_change_pw(email, password, no_password_change=True):
    """Change a user's password."""
    args = ["user", "change_pw", email, password]
    if no_password_change:
        args.append("--no_password_change")
    return run(*args)


def user_change_authkey(email, key=None):
    """Change a user's authkey. Returns the new key."""
    args = ["user", "change_authkey", email]
    if key:
        args.append(key)
    _, stdout = run(*args)
    return stdout
