"""Admin user, organisation and API key setup."""

from __future__ import annotations

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

    # Set admin password: only when it differs, since a change saves the user, writes an
    # audit row and fires MISP's user-after-save workflow trigger
    password = env("ADMIN_PASSWORD")
    if password and not has_password(1, password):
        log.info("setting admin password (no forced reset)")
        rc, out = cake.user_change_pw(admin_email, password)
        if rc != 0:
            log.error("failed to set admin password: %s", out)
        else:
            db.execute(f"UPDATE users SET change_pw = FALSE, last_pw_change = {db.NOW_EPOCH} WHERE id = 1")

    # Set admin API key
    api_key = env("ADMIN_KEY")
    if api_key:
        _set_admin_authkey(admin_email, api_key)


# -- Internal helpers --


# The description MISP gives the organisation it generates for its first user (User.php)
GENERATED_ORG_DESCRIPTION = "Automatically generated admin organisation"


def _org_id_by_uuid(org_uuid: str):
    row = _one("SELECT id FROM organisations WHERE uuid = %s", (org_uuid,))
    return row["id"] if row else None


def _configure_admin_org(admin_org: str) -> None:
    """The admin's organisation, by ADMIN_ORG_UUID or else the one of user 1, as MISP's host organisation."""
    org_uuid = env("ADMIN_ORG_UUID")
    if org_uuid:
        org_id = _org_id_by_uuid(org_uuid)
        if not org_id:
            # MISP names the organisation it generates for its first user after
            # MISP.org, which ADMIN_ORG fills (settings.yaml derived): that one becomes the admin org
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
                "VALUES (%s, %s, TRUE, NOW(), NOW(), '', '', '', '', 0)",
                (admin_org, org_uuid),
            )
            org_id = _org_id_by_uuid(org_uuid)
        row = _one("SELECT org_id FROM users WHERE id = 1")
        if org_id and str((row or {}).get("org_id")) != str(org_id):
            log.info("assigning admin user to org id=%s (uuid=%s)", org_id, org_uuid)
            db.execute("UPDATE users SET org_id = %s WHERE id = 1", (org_id,))
    else:
        org_id = (_one("SELECT org_id FROM users WHERE id = 1") or {}).get("org_id")
    if not org_id:
        return
    if admin_org != "ORGNAME" and db.execute(
            "UPDATE organisations SET name = %s, date_modified = NOW() WHERE id = %s AND name != %s",
            (admin_org, org_id, admin_org)):
        log.info("named the admin organisation %s (id=%s)", admin_org, org_id)
    # Each set is a PHP process and a log row in MISP, so only a change
    if str(db.get_system_setting("MISP.host_org_id")) != str(org_id):
        log.info("making organisation %s MISP's host organisation", org_id)
        cake.set_setting("MISP.host_org_id", org_id)


def has_authkey(user_id: int, authkey: str) -> bool:
    """Whether a live auth_keys row of the user already holds this key."""
    import bcrypt

    # MISP keeps the first and last four characters of a key next to its hash
    rows = db.dict_query(
        "SELECT authkey FROM auth_keys WHERE user_id = %s AND authkey_start = %s AND authkey_end = %s "
        f"AND (expiration = 0 OR expiration > {db.NOW_EPOCH})",
        (user_id, authkey[:4], authkey[-4:]),
    )
    for row in rows:
        # MISP writes PHP's $2y$ prefix; the bcrypt module checks $2b$
        stored = str(row["authkey"]).replace("$2y$", "$2b$", 1).encode()
        if bcrypt.checkpw(authkey.encode(), stored):
            return True
    return False


def has_password(user_id: int, password: str) -> bool:
    """Whether the stored password hash of the user verifies this password."""
    import bcrypt

    rows = db.dict_query("SELECT password FROM users WHERE id = %s", (user_id,))
    stored = str(rows[0]["password"]) if rows else ""
    if not stored.startswith("$2"):
        return False
    # MISP writes PHP's $2y$ prefix; the bcrypt module checks $2b$
    return bcrypt.checkpw(password.encode(), stored.replace("$2y$", "$2b$", 1).encode())


def _set_admin_authkey(email: str, api_key: str) -> None:
    """Set the admin's API key, unless a live key of user 1 already is this one."""
    if not has_authkey(1, api_key):
        log.info("setting admin API key")
        cake.user_change_authkey(email, api_key)
