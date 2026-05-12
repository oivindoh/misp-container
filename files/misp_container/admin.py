"""Admin user, GPG, auth, and service configuration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import MISP_BASE
from . import cake
from . import db
from .env import env
from .log import get as getlog

log = getlog("admin")


def _sql_escape(value: str) -> str:
    """Escape a string for safe use in SQL single-quoted literals."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")


def setup_admin() -> None:
    """Configure admin user: email, org, password, API key."""
    admin_email = env("ADMIN_EMAIL")
    admin_org = env("ADMIN_ORG")

    # Initialize default user/role/org if fresh database
    log.info("ensuring default admin user exists")
    cake.user_init()

    # Change admin email if different from default
    if admin_email != "admin@admin.test":
        current = db.query("SELECT email FROM users WHERE id=1;").strip()
        if current != admin_email:
            log.info("changing admin email to %s", admin_email)
            db.query(f"UPDATE users SET email='{_sql_escape(admin_email)}' WHERE id=1;")

    # Configure admin organisation
    _configure_admin_org(admin_org)

    # Set admin password
    password = _read_secret("ADMIN_PASSWORD", "ADMIN_PASSWORD_FILE")
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
                db.query("UPDATE users SET change_pw=0, last_pw_change=UNIX_TIMESTAMP() WHERE id=1;")

    # Set admin API key
    api_key = _read_secret("ADMIN_KEY", "ADMIN_KEY_FILE")
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

        tmp = Path("/tmp/gpg.tmp")
        tmp.write_text(
            f"%echo Generating a basic OpenPGP key\n"
            f"Key-Type: RSA\nKey-Length: 3072\n"
            f"Name-Real: MISP Admin\nName-Email: {env('MISP_EMAIL', env('ADMIN_EMAIL'))}\n"
            f"Expire-Date: 0\nPassphrase: {env('GNUPG_PASSWORD')}\n"
            f"%commit\n%echo Done\n"
        )
        subprocess.run(
            ["gpg", "--homedir", str(gpg_dir), "--gen-key", "--batch", str(tmp)],
            check=True,
        )
        tmp.unlink(missing_ok=True)
    else:
        log.info("found pre-generated GPG key in %s", gpg_dir)

    # GPG public key is served via MISP.download_gpg_from_homedir=true
    # (reads directly from .gnupg/ volume), so no need to export gpg.asc
    # to the read-only webroot.


def configure_oidc() -> None:
    """Configure OIDC authentication if enabled."""
    if env("OIDC_ENABLE") != "true":
        return

    log.info("enabling OIDC authentication")
    cake.set_setting("Plugin.CustomAuth_enable", "true")
    cake.set_setting("Plugin.CustomAuth_name", "OpenIDConnect")
    cake.set_setting("Plugin.CustomAuth_custom_password_reset", f"{env('OIDC_PROVIDER_URL')}/account")

    _apply_optional_settings({
        "OIDC_PROVIDER_URL": "OidcAuth.provider_url",
        "OIDC_ISSUER": "OidcAuth.issuer",
        "OIDC_CLIENT_ID": "OidcAuth.client_id",
        "OIDC_CLIENT_SECRET": "OidcAuth.client_secret",
        "OIDC_ROLES_PROPERTY": "OidcAuth.roles_property",
        "OIDC_ROLES_MAPPING": "OidcAuth.role_mapper",
        "OIDC_DEFAULT_ORG": "OidcAuth.default_org",
        "OIDC_REDIRECT_URI": "OidcAuth.redirect_uri",
        "OIDC_SCOPES": "OidcAuth.scopes",
        "OIDC_LOGOUT_URL": "OidcAuth.logout_url",
    })
    _apply_settings_with_defaults({
        "OidcAuth.code_challenge_method": ("OIDC_CODE_CHALLENGE_METHOD", "S256"),
        "OidcAuth.unblock": ("OIDC_MIXEDAUTH", "false"),
        "OidcAuth.authentication_method": ("OIDC_AUTH_METHOD", "client_secret_post"),
        "OidcAuth.disable_request_object": ("OIDC_DISABLE_REQUEST_OBJECT", "false"),
        "OidcAuth.skip_proxy": ("OIDC_SKIP_PROXY", "true"),
    })


def configure_ldap() -> None:
    """Configure LDAP authentication if enabled."""
    if not any(env(k) == "true" for k in ("LDAP_ENABLE", "LDAPAUTH_ENABLE", "APACHESECUREAUTH_LDAP_ENABLE")):
        return

    log.info("enabling LDAP authentication")

    if env("LDAPAUTH_ENABLE") == "true":
        _apply_optional_settings({
            "LDAPAUTH_LDAPSERVER": "LdapAuth.ldapServer",
            "LDAPAUTH_LDAPDN": "LdapAuth.ldapDn",
            "LDAPAUTH_LDAPREADERUSER": "LdapAuth.ldapReaderUser",
            "LDAPAUTH_LDAPREADERPASSWORD": "LdapAuth.ldapReaderPassword",
            "LDAPAUTH_LDAPSEARCHFILTER": "LdapAuth.ldapSearchFilter",
            "LDAPAUTH_LDAPSEARCHATTRIBUTE": "LdapAuth.ldapSearchAttribute",
            "LDAPAUTH_LDAPEMAILFIELD": "LdapAuth.ldapEmailField",
            "LDAPAUTH_LDAPDEFAULTORGID": "LdapAuth.ldapDefaultOrgId",
            "LDAPAUTH_LDAPDEFAULTROLEID": "LdapAuth.ldapDefaultRoleId",
        })
        _apply_settings_with_defaults({
            "LdapAuth.starttls": ("LDAPAUTH_STARTTLS", "false"),
            "LdapAuth.mixedAuth": ("LDAPAUTH_MIXEDAUTH", "true"),
            "LdapAuth.updateUser": ("LDAPAUTH_UPDATEUSER", "true"),
            "LdapAuth.debug": ("LDAPAUTH_DEBUG", "false"),
        })

    if env("APACHESECUREAUTH_LDAP_ENABLE") == "true":
        cake.set_setting("ApacheSecureAuth.apacheEnv", env("APACHESECUREAUTH_LDAP_APACHE_ENV"))
        _apply_optional_settings({
            "APACHESECUREAUTH_LDAP_SERVER": "ApacheSecureAuth.ldapServer",
            "APACHESECUREAUTH_LDAP_READER_USER": "ApacheSecureAuth.ldapReaderUser",
            "APACHESECUREAUTH_LDAP_READER_PASSWORD": "ApacheSecureAuth.ldapReaderPassword",
            "APACHESECUREAUTH_LDAP_DN": "ApacheSecureAuth.ldapDN",
            "APACHESECUREAUTH_LDAP_SEARCH_ATTRIBUTE": "ApacheSecureAuth.ldapSearchAttribute",
            "APACHESECUREAUTH_LDAP_FILTER": "ApacheSecureAuth.ldapFilter",
            "APACHESECUREAUTH_LDAP_DEFAULT_ROLE_ID": "ApacheSecureAuth.ldapDefaultRoleId",
            "APACHESECUREAUTH_LDAP_DEFAULT_ORG": "ApacheSecureAuth.ldapDefaultOrg",
            "APACHESECUREAUTH_LDAP_EMAIL_FIELD": "ApacheSecureAuth.ldapEmailField",
        })
        cake.set_setting("ApacheSecureAuth.starttls", env("APACHESECUREAUTH_LDAP_STARTTLS"))


def configure_custom_auth() -> None:
    """Configure custom header authentication (oauth2-proxy / reverse proxy)."""
    if env("CUSTOM_AUTH_ENABLE") != "true":
        return

    log.info("enabling custom header authentication")
    _apply_settings_with_defaults({
        "Plugin.CustomAuth_enable": (None, "true"),
        "Plugin.CustomAuth_header": ("CUSTOM_AUTH_HEADER", "X_FORWARDED_EMAIL"),
        "Plugin.CustomAuth_use_header_namespace": ("CUSTOM_AUTH_USE_HEADER_NAMESPACE", "true"),
        "Plugin.CustomAuth_required": ("CUSTOM_AUTH_REQUIRED", "false"),
        "Plugin.CustomAuth_header_namespace": ("CUSTOM_AUTH_HEADER_NAMESPACE", "HTTP_"),
        "Plugin.CustomAuth_name": ("CUSTOM_AUTH_NAME", "External Authentication"),
        "Plugin.CustomAuth_disable_logout": ("CUSTOM_AUTH_DISABLE_LOGOUT", "false"),
    })
    _apply_optional_settings({
        "CUSTOM_AUTH_ONLY_ALLOW_SOURCE": "Plugin.CustomAuth_only_allow_source",
        "CUSTOM_AUTH_CUSTOM_PASSWORD_RESET": "Plugin.CustomAuth_custom_password_reset",
        "CUSTOM_AUTH_CUSTOM_LOGOUT": "Plugin.CustomAuth_custom_logout",
    })



# -- Internal helpers --


def _apply_optional_settings(mapping: dict[str, str]) -> None:
    """Apply settings only if the corresponding env var is non-empty.

    mapping: {env_var_name: misp_setting_name}
    """
    for env_key, setting in mapping.items():
        val = env(env_key)
        if val:
            cake.set_setting(setting, val)


def _apply_settings_with_defaults(mapping: dict[str, tuple[str | None, str]]) -> None:
    """Apply settings using env var with a fallback default.

    mapping: {misp_setting_name: (env_var_name_or_None, default_value)}
    If env_var_name is None, uses the default directly.
    """
    for setting, (env_key, default) in mapping.items():
        val = env(env_key, default) if env_key else default
        cake.set_setting(setting, val)


def _configure_admin_org(admin_org: str) -> None:
    """Configure admin organisation by UUID or name."""
    org_uuid = _sql_escape(env("ADMIN_ORG_UUID"))
    safe_org = _sql_escape(admin_org)
    if org_uuid:
        org_id = db.query(f"SELECT id FROM organisations WHERE uuid='{org_uuid}';").strip()
        if not org_id:
            log.info("creating organisation '%s' with UUID %s", admin_org, org_uuid)
            db.query(
                f"INSERT INTO organisations (name, uuid, local, date_created, date_modified, "
                f"description, type, nationality, sector, created_by) "
                f"VALUES ('{safe_org}', '{org_uuid}', 1, NOW(), NOW(), '', '', '', '', 0);"
            )
            org_id = db.query(f"SELECT id FROM organisations WHERE uuid='{org_uuid}';").strip()

        if org_id:
            if admin_org != "ORGNAME":
                log.info("setting org name to %s (id=%s)", admin_org, org_id)
                db.query(
                    f"UPDATE organisations SET name='{safe_org}', date_modified=NOW() "
                    f"WHERE id={org_id} AND name != '{safe_org}';"
                )
            current_org = db.query("SELECT org_id FROM users WHERE id=1;").strip()
            if current_org != org_id:
                log.info("assigning admin user to org id=%s (uuid=%s)", org_id, org_uuid)
                db.query(f"UPDATE users SET org_id={org_id} WHERE id=1;")
            cake.set_setting("MISP.host_org_id", org_id)

    elif admin_org != "ORGNAME":
        log.info("setting admin org name to %s", admin_org)
        db.query(
            f"UPDATE organisations SET name='{safe_org}', date_modified=NOW() "
            f"WHERE id=1 AND name != '{safe_org}';"
        )


def _set_admin_authkey(email: str, api_key: str) -> None:
    """Set admin API key, skipping if it already exists (idempotent)."""
    key_start = _sql_escape(api_key[:4])
    key_end = _sql_escape(api_key[36:40] if len(api_key) >= 40 else "")
    count = db.query(
        f"SELECT COUNT(*) FROM auth_keys WHERE user_id=1 "
        f"AND authkey_start='{key_start}' AND authkey_end='{key_end}' "
        f"AND (expiration = 0 OR expiration > UNIX_TIMESTAMP());"
    ).strip()
    if count == "0" or not count:
        log.info("setting admin API key")
        cake.user_change_authkey(email, api_key)


def _read_secret(env_key: str, file_key: str) -> str:
    """Read a secret from a file (if FILE env set) or from the env var directly."""
    file_path = env(file_key)
    if file_path and os.path.isfile(file_path):
        return Path(file_path).read_text().strip()
    return env(env_key)
