"""Thin wrapper around the CakePHP cake CLI."""

import subprocess

from . import CAKE
from .log import get as getlog

log = getlog("cake")


def run(*args, quiet=True, check=False):
    """Run a cake command. Returns (returncode, stdout)."""
    cmd = [CAKE] + list(args)
    if quiet and args and args[0] == "Admin" and len(args) > 1 and args[1] == "setSetting":
        cmd.insert(cmd.index("setSetting") + 1, "-q")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"cake {' '.join(args)} failed: {result.stderr.strip()}")
    return result.returncode, result.stdout.strip()


def set_setting(setting, value, force=False):
    """Set a MISP setting via cake Admin setSetting."""
    args = ["Admin", "setSetting", "-q"]
    if force:
        args.append("-f")
    args.extend([setting, str(value)])
    result = subprocess.run([CAKE] + args, capture_output=True, text=True)
    return result.returncode == 0


def get_setting(setting):
    """Get a MISP setting via cake Admin getSetting. Returns the raw JSON output."""
    _, stdout = run("Admin", "getSetting", setting)
    return stdout


def run_updates():
    """Run MISP database updates."""
    log.info("running MISP database updates")
    run("Admin", "runUpdates")


def run_db_script(name):
    """Run a MISP database script (idempotent)."""
    log.info("running DB script: %s", name)
    run("Admin", "runDbScript", name)


def user_init():
    """Initialize default user/role/org."""
    run("user", "init")


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
