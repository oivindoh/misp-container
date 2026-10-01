"""Environment variable handling.

All defaults live in two places:
1. settings.yaml -- for MISP settings (auto-derived env vars)
2. deploy/chart/files/*.env -- for container config (MySQL, PHP, etc.)

apply_defaults() loads settings.yaml defaults into os.environ so that
env("MISP_REDIS_HOST") works everywhere without inline defaults.

Docker Compose (env_file:) and the Helm chart (the misp-env ConfigMap and the
Secrets) load the env files before the container starts, so those values are
already in os.environ.
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


# Database connection: DB_* is the engine-neutral form; MYSQL_* stays as the
# alias every existing deployment sets.
DB_ALIASES = {
    "MYSQL_HOST": "DB_HOST",
    "MYSQL_PORT": "DB_PORT",
    "MYSQL_DATABASE": "DB_NAME",
    "MYSQL_USER": "DB_USER",
    "MYSQL_PASSWORD": "DB_PASSWORD",
    "MYSQL_TLS": "DB_TLS",
}

# Documented short names for the auth plugins, mapped onto the env vars derived
# from the setting names (OidcAuth.provider_url -> OIDCAUTH_PROVIDER_URL).
ALIASES = {
    "OIDC_PROVIDER_URL": "OIDCAUTH_PROVIDER_URL",
    "OIDC_ISSUER": "OIDCAUTH_ISSUER",
    "OIDC_CLIENT_ID": "OIDCAUTH_CLIENT_ID",
    "OIDC_CLIENT_SECRET": "OIDCAUTH_CLIENT_SECRET",
    "OIDC_ROLES_PROPERTY": "OIDCAUTH_ROLES_PROPERTY",
    "OIDC_ROLES_MAPPING": "OIDCAUTH_ROLE_MAPPER",
    "OIDC_DEFAULT_ORG": "OIDCAUTH_DEFAULT_ORG",
    "OIDC_SCOPES": "OIDCAUTH_SCOPES",
    "OIDC_CODE_CHALLENGE_METHOD": "OIDCAUTH_CODE_CHALLENGE_METHOD",
    "OIDC_AUTH_METHOD": "OIDCAUTH_AUTHENTICATION_METHOD",
    "OIDC_MIXEDAUTH": "OIDCAUTH_MIXEDAUTH",
    "OIDC_DISABLE_REQUEST_OBJECT": "OIDCAUTH_DISABLE_REQUEST_OBJECT",
    "OIDC_SKIP_PROXY": "OIDCAUTH_SKIPPROXY",
    "LDAP_ENABLE": "LDAPAUTH_ENABLE",
    "APACHESECUREAUTH_LDAP_APACHE_ENV": "APACHESECUREAUTH_APACHEENV",
    "APACHESECUREAUTH_LDAP_SERVER": "APACHESECUREAUTH_LDAPSERVER",
    "APACHESECUREAUTH_LDAP_READER_USER": "APACHESECUREAUTH_LDAPREADERUSER",
    "APACHESECUREAUTH_LDAP_READER_PASSWORD": "APACHESECUREAUTH_LDAPREADERPASSWORD",
    "APACHESECUREAUTH_LDAP_DN": "APACHESECUREAUTH_LDAPDN",
    "APACHESECUREAUTH_LDAP_SEARCH_ATTRIBUTE": "APACHESECUREAUTH_LDAPSEARCHATTRIBUTE",
    "APACHESECUREAUTH_LDAP_FILTER": "APACHESECUREAUTH_LDAPFILTER",
    "APACHESECUREAUTH_LDAP_DEFAULT_ROLE_ID": "APACHESECUREAUTH_LDAPDEFAULTROLEID",
    "APACHESECUREAUTH_LDAP_DEFAULT_ORG": "APACHESECUREAUTH_LDAPDEFAULTORG",
    "APACHESECUREAUTH_LDAP_EMAIL_FIELD": "APACHESECUREAUTH_LDAPEMAILFIELD",
    "APACHESECUREAUTH_LDAP_STARTTLS": "APACHESECUREAUTH_STARTTLS",
    # Header authentication (the custom_auth group of settings.yaml)
    "CUSTOM_AUTH_ENABLE": "PLUGIN_CUSTOMAUTH_ENABLE",
    "CUSTOM_AUTH_HEADER": "PLUGIN_CUSTOMAUTH_HEADER",
    "CUSTOM_AUTH_USE_HEADER_NAMESPACE": "PLUGIN_CUSTOMAUTH_USE_HEADER_NAMESPACE",
    "CUSTOM_AUTH_REQUIRED": "PLUGIN_CUSTOMAUTH_REQUIRED",
    "CUSTOM_AUTH_HEADER_NAMESPACE": "PLUGIN_CUSTOMAUTH_HEADER_NAMESPACE",
    "CUSTOM_AUTH_NAME": "PLUGIN_CUSTOMAUTH_NAME",
    "CUSTOM_AUTH_DISABLE_LOGOUT": "PLUGIN_CUSTOMAUTH_DISABLE_LOGOUT",
    "CUSTOM_AUTH_ONLY_ALLOW_SOURCE": "PLUGIN_CUSTOMAUTH_ONLY_ALLOW_SOURCE",
    "CUSTOM_AUTH_CUSTOM_PASSWORD_RESET": "PLUGIN_CUSTOMAUTH_CUSTOM_PASSWORD_RESET",
    "CUSTOM_AUTH_CUSTOM_LOGOUT": "PLUGIN_CUSTOMAUTH_CUSTOM_LOGOUT",
    # The logout redirect is one setting whichever plugin logs the user in
    "OIDC_LOGOUT_URL": "PLUGIN_CUSTOMAUTH_CUSTOM_LOGOUT",
}


def apply_defaults():
    """Apply runtime defaults that can't live in env files.

    MISP setting defaults live in settings.yaml (loaded by the config engine).
    Container config defaults live in base.env (read by the Helm chart and Compose).
    This function handles the derived variables and the aliases.
    """
    for source, targets in DERIVED.items():
        value = os.environ.get(source, "")
        if value:
            for target in targets:
                os.environ.setdefault(target, value)
    for alias, target in ALIASES.items():
        value = os.environ.get(alias, "")
        if value:
            os.environ.setdefault(target, value)
    for alias, target in DB_ALIASES.items():
        value = os.environ.get(alias, "")
        if value:
            os.environ.setdefault(target, value)
    os.environ.setdefault("DB_ENGINE", "mysql")
    misp_email = os.environ.get("MISP_EMAIL") or os.environ.get("ADMIN_EMAIL", "")
    if misp_email:
        os.environ.setdefault("MISP_CONTACT", misp_email)
        os.environ.setdefault("GNUPG_EMAIL", misp_email)
