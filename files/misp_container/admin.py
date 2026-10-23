"""Admin user and GPG key setup."""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import cake
from . import db
from .env import env
from .log import get as getlog

log = getlog("admin")


def _one(sql: str, params=()) -> dict | None:
    """The first row of a query, or None."""
    rows = db.dict_query(sql, params)
    return rows[0] if rows else None


def setup_admin() -> None:
    """Configure admin user: email, org, password, API key."""
    admin_email = env("ADMIN_EMAIL")
    admin_org = env("ADMIN_ORG")

    # Initialize default user/role/org if fresh database
    log.info("ensuring default admin user exists")
    cake.user_init()

    # Change admin email if different from default
    if admin_email != "admin@admin.test":
        row = _one("SELECT email FROM users WHERE id = 1")
        if (row or {}).get("email") != admin_email:
            log.info("changing admin email to %s", admin_email)
            db.execute("UPDATE users SET email = %s WHERE id = 1", (admin_email,))

    _configure_admin_org(admin_org)

    # Set admin password
    password = env("ADMIN_PASSWORD")
    if password:
        if len(password) < 12:
            log.error("ADMIN_PASSWORD is too short (%d chars). MISP requires at least 12 "
                      "characters. Login will not work until a valid password is set.", len(password))
        else:
            log.info("setting admin password (no forced reset)")
            rc, out = cake.user_change_pw(admin_email, password)
            if rc != 0:
                log.error("failed to set admin password: %s", out)
            else:
                db.execute(f"UPDATE users SET change_pw = {db.bool_lit(False)}, "
                           f"last_pw_change = {db.now_epoch()} WHERE id = 1")

    # Set admin API key
    api_key = env("ADMIN_KEY")
    if api_key:
        _set_admin_authkey(admin_email, api_key)


def configure_gnupg() -> None:
    """Generate GPG key if not present."""
    if env("AUTOCONF_GPG") != "true":
        log.info("GPG auto configuration disabled")
        return

    gpg_dir = Path(env("GNUPG_HOMEDIR"))

    if not (gpg_dir / "trustdb.gpg").exists():
        log.info("generating new GPG key in %s", gpg_dir)
        gpg_dir.mkdir(parents=True, exist_ok=True)
        try:
            gpg_dir.chmod(0o700)
        except PermissionError:
            pass  # K8s emptyDir: mount point chmod not allowed, permissions are fine

        # The parameters carry the passphrase, so they go over stdin, never a file
        params = (
            "%echo Generating a basic OpenPGP key\n"
            "Key-Type: RSA\nKey-Length: 3072\n"
            f"Name-Real: MISP Admin\nName-Email: {env('MISP_EMAIL', env('ADMIN_EMAIL'))}\n"
            f"Expire-Date: 0\nPassphrase: {env('GNUPG_PASSWORD')}\n"
            "%commit\n%echo Done\n"
        )
        subprocess.run(
            ["gpg", "--homedir", str(gpg_dir), "--gen-key", "--batch"],
            input=params, text=True, check=True,
        )
    else:
        log.info("found pre-generated GPG key in %s", gpg_dir)

    # GPG public key is served via MISP.download_gpg_from_homedir=true
    # (reads directly from .gnupg/ volume), so no need to export gpg.asc
    # to the read-only webroot.


# -- Internal helpers --


# The description MISP gives the organisation it generates for its first user (User.php)
GENERATED_ORG_DESCRIPTION = "Automatically generated admin organisation"


def _org_id_by_uuid(org_uuid: str):
    row = _one("SELECT id FROM organisations WHERE uuid = %s", (org_uuid,))
    return row["id"] if row else None


def _configure_admin_org(admin_org: str) -> None:
    """Configure admin organisation by UUID or name."""
    org_uuid = env("ADMIN_ORG_UUID")
    if org_uuid:
        org_id = _org_id_by_uuid(org_uuid)
        if not org_id:
            # MISP names the organisation it generates for its first user after
            # MISP.org, which ADMIN_ORG fills (env.py): that one becomes the admin org
            generated = _one("SELECT id FROM organisations WHERE name = %s AND type = 'ADMIN' AND description = %s",
                             (admin_org, GENERATED_ORG_DESCRIPTION))
            if generated:
                org_id = generated["id"]
                log.info("giving the organisation MISP generated, '%s', the UUID %s", admin_org, org_uuid)
                db.execute("UPDATE organisations SET uuid = %s, date_modified = NOW() WHERE id = %s",
                           (org_uuid, org_id))
        if not org_id:
            log.info("creating organisation '%s' with UUID %s", admin_org, org_uuid)
            db.execute(
                "INSERT INTO organisations (name, uuid, local, date_created, date_modified, "
                "description, type, nationality, sector, created_by) "
                f"VALUES (%s, %s, {db.bool_lit(True)}, NOW(), NOW(), '', '', '', '', 0)",
                (admin_org, org_uuid),
            )
            org_id = _org_id_by_uuid(org_uuid)

        if org_id:
            if admin_org != "ORGNAME":
                log.info("setting org name to %s (id=%s)", admin_org, org_id)
                db.execute("UPDATE organisations SET name = %s, date_modified = NOW() WHERE id = %s AND name != %s",
                           (admin_org, org_id, admin_org))
            row = _one("SELECT org_id FROM users WHERE id = 1")
            if str((row or {}).get("org_id")) != str(org_id):
                log.info("assigning admin user to org id=%s (uuid=%s)", org_id, org_uuid)
                db.execute("UPDATE users SET org_id = %s WHERE id = 1", (org_id,))
            cake.set_setting("MISP.host_org_id", org_id)

    elif admin_org != "ORGNAME":
        log.info("setting admin org name to %s", admin_org)
        db.execute("UPDATE organisations SET name = %s, date_modified = NOW() WHERE id = 1 AND name != %s",
                   (admin_org, admin_org))


def has_authkey(user_id: int, authkey: str) -> bool:
    """Whether a live auth_keys row of the user already holds this key."""
    import bcrypt

    # MISP keeps the first and last four characters of a key next to its hash
    rows = db.dict_query(
        "SELECT authkey FROM auth_keys WHERE user_id = %s AND authkey_start = %s AND authkey_end = %s "
        f"AND (expiration = 0 OR expiration > {db.now_epoch()})",
        (user_id, authkey[:4], authkey[-4:]),
    )
    for row in rows:
        # MISP writes PHP's $2y$ prefix; the bcrypt module checks $2b$
        stored = str(row["authkey"]).replace("$2y$", "$2b$", 1).encode()
        if bcrypt.checkpw(authkey.encode(), stored):
            return True
    return False


def _set_admin_authkey(email: str, api_key: str) -> None:
    """Set the admin's API key, unless a live key of user 1 already is this one."""
    if not has_authkey(1, api_key):
        log.info("setting admin API key")
        cake.user_change_authkey(email, api_key)
