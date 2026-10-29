"""The repository's settings as the chart renders them (_settings.tpl) and a pod reads them."""

import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "files"))

import chart  # noqa: E402
from misp_container.config import config_php_values, render_config_php  # noqa: E402

SETTINGS = yaml.safe_load((REPO / "deploy/chart/files/settings.yaml").read_text())
PLUGIN_GROUPS = {"oidc", "ldap"}


def rendered(*sets: str) -> dict[str, str]:
    """The env of a pod that runs MISP: misp-settings, plus the settings a Secret holds, by reference."""
    data = dict(chart.find("ConfigMap", "misp-settings", *sets)["data"])
    web = chart.find("Deployment", "web", *sets)
    php = next(c for c in chart.pod_spec(web)["containers"] if c["name"] == "php-fpm")
    for entry in php["env"]:
        ref = entry.get("valueFrom", {}).get("secretKeyRef")
        if ref and entry["name"].startswith(("MISP_SETTING_", "MISP_CONFIG_")):
            data[entry["name"]] = f"<{ref['name']}/{ref['key']}>"
    return data


def config_php(*sets: str) -> str:
    return render_config_php(config_php_values(rendered(*sets)))


class TestTheFile:
    def test_each_setting_is_in_one_group(self):
        counts = Counter(name for group in SETTINGS["settings"].values() for name in group)
        assert [name for name, n in counts.items() if n > 1] == ["Security.auth"]

    def test_misp_takes_every_category_from_its_env_layer(self):
        # SystemSetting::ALLOWED_CATEGORIES; the env-settings check of check_upstream.py watches it
        allowed = {"SimpleBackgroundJobs", "MISP", "Security", "GnuPG", "SMIME", "Proxy", "SecureAuth",
                   "Session", "Plugin", "debug", "site_admin_debug"}
        categories = {name.split(".")[0] for group, settings in SETTINGS["settings"].items()
                      if group not in PLUGIN_GROUPS for name in settings}
        assert categories <= allowed


class TestTheEnvLayer:
    def test_every_setting_reaches_misps_env_layer(self):
        env = rendered()
        carried = set(env["MISP_REPOSITORY_SETTINGS"].split(","))
        names = {name for group, settings in SETTINGS["settings"].items()
                 if group not in PLUGIN_GROUPS | {"s3"} for name in settings}
        with_value = {name for group, settings in SETTINGS["settings"].items()
                      if group not in PLUGIN_GROUPS | {"s3"} for name, spec in settings.items()
                      if spec.get("value") not in ("", None) and not spec.get("secret")}
        # A blank-protected empty default stays MISP's own until env or a Secret gives a value
        assert with_value <= carried <= names
        for name in carried:
            assert f"MISP_SETTING_{name.replace('.', '__')}" in env, name

    def test_debug_is_in_the_env_layer_too(self):
        # MISP takes debug as a database row, which would win over config.php
        assert rendered()["MISP_SETTING_debug"] == "0"

    def test_an_empty_env_value_clears_a_setting(self):
        assert rendered("env.MISP_EVENT_VIEW_FILTER_FIELDS=")["MISP_SETTING_MISP__event_view_filter_fields"] == ""
        assert "MISP_SETTING_MISP__welcome_text_top" not in rendered("env.MISP_WELCOME_TEXT_TOP=")

    def test_a_large_number_keeps_its_digits(self):
        # toString would print 1e+06, which PHP refuses as a limit
        data = chart.find("ConfigMap", "misp-env", "--set-json=env.PHP_MEMORY_LIMIT=1500000")["data"]
        assert data["PHP_MEMORY_LIMIT"] == "1500000"

    def test_a_trailing_slash_leaves_the_baseurl(self):
        env = rendered("env.MISP_BASEURL=https://misp.example.org/")
        assert env["MISP_SETTING_MISP__baseurl"] == env["MISP_SETTING_MISP__external_baseurl"] == "https://misp.example.org"

    def test_an_https_baseurl_forces_https(self):
        # PHP sees plain HTTP from caddy; with force_https MISP marks the session cookie Secure
        assert rendered("env.MISP_BASEURL=https://misp.example.org")["MISP_SETTING_Security__force_https"] == "true"
        assert rendered("env.MISP_BASEURL=http://localhost:8080")["MISP_SETTING_Security__force_https"] == "false"
        assert rendered("env.MISP_BASEURL=https://misp.example.org",
                        "env.SECURITY_FORCE_HTTPS=false")["MISP_SETTING_Security__force_https"] == "false"

    def test_an_env_value_replaces_the_default(self):
        assert rendered("env.MISP_TITLE_TEXT=Example")["MISP_SETTING_MISP__title_text"] == "Example"

    def test_a_secret_setting_comes_from_its_secret(self):
        env = rendered()
        assert env["MISP_SETTING_GnuPG__password"] == "<misp-app/GNUPG_PASSWORD>"
        assert env["MISP_SETTING_Security__salt"] == "<misp-app/SECURITY_SALT>"
        secret = {f"MISP_SETTING_{name.replace('.', '__')}" for group in SETTINGS["settings"].values()
                  for name, spec in group.items() if spec.get("secret")}
        assert not secret & set(chart.find("ConfigMap", "misp-settings")["data"])

    def test_a_supplied_secret_needs_no_entry_in_the_secrets_value(self):
        # With secrets.create false the release's Secret may hold OIDC_CLIENT_SECRET alone
        env = rendered("secrets.create=false", "env.OIDC_ENABLE=true")
        assert env["MISP_CONFIG_OidcAuth__client_secret"] == "<misp-app/OIDC_CLIENT_SECRET>"
        assert "MISP_CONFIG_OidcAuth__client_secret" in env["MISP_PLAIN_CONFIG"].split(",")

    @pytest.mark.parametrize("value", ["env.PLUGIN_S3_AWS_SECRET_KEY=s", "env.OIDC_CLIENT_SECRET=s"])
    def test_a_secret_in_env_is_refused(self, value):
        with pytest.raises(RuntimeError, match="secrets.app"):
            chart.find("ConfigMap", "misp-settings", value)

    def test_a_listed_setting_by_its_direct_name_is_refused(self):
        with pytest.raises(RuntimeError, match="use env.MISP_TITLE_TEXT"):
            chart.find("ConfigMap", "misp-settings", "env.MISP_SETTING_MISP__title_text=x")

    def test_a_connection_secret_feeds_the_derived_redis_passwords(self):
        env = rendered("connectionSecrets[0].name=cache", "connectionSecrets[0].keys.MISP_REDIS_PASSWORD=pw")
        assert env["MISP_SETTING_SimpleBackgroundJobs__redis_password"] == "<cache/pw>"


class TestDerivedNames:
    def test_the_redis_mirrors_inherit(self):
        env = rendered("env.MISP_REDIS_HOST=cache", "env.MISP_REDIS_PORT=6380")
        assert env["MISP_SETTING_SimpleBackgroundJobs__redis_host"] == "cache"
        assert env["MISP_SETTING_Plugin__ZeroMQ_redis_port"] == "6380"

    def test_an_explicit_value_wins(self):
        env = rendered("env.MISP_REDIS_HOST=cache", "env.SIMPLEBACKGROUNDJOBS_REDIS_HOST=jobs")
        assert env["MISP_SETTING_SimpleBackgroundJobs__redis_host"] == "jobs"

    def test_the_admin_email_reaches_misps_own_addresses(self):
        env = rendered("env.ADMIN_EMAIL=ops@example.org")
        assert {env[f"MISP_SETTING_{s}"] for s in ("MISP__email", "MISP__contact", "GnuPG__email")} == {"ops@example.org"}

    def test_the_baseurl_and_the_admin_org_inherit(self):
        env = rendered("env.MISP_BASEURL=https://misp.example.org", "env.ADMIN_ORG=Example Org")
        assert env["MISP_SETTING_MISP__external_baseurl"] == "https://misp.example.org"
        assert env["MISP_SETTING_MISP__org"] == "Example Org"


class TestConfigPhp:
    def test_config_php_holds_only_what_misp_reads_before_its_env_layer(self):
        out = config_php()
        assert "'system_setting_db' => true," in out
        assert "redis_host" not in out and "debug" not in out and "title_text" not in out

    def test_a_bucket_points_attachments_at_s3(self):
        env = rendered("env.PLUGIN_S3_BUCKET_NAME=misp")
        assert env["MISP_SETTING_MISP__attachments_dir"] == "s3://"
        assert env["MISP_SETTING_Plugin__S3_bucket_name"] == "misp"

    def test_a_string_setting_stays_a_string(self):
        out = config_php("env.OIDC_ENABLE=true", "env.OIDC_CLIENT_ID=true", "env.LDAPAUTH_ENABLE=true")
        assert "'client_id' => 'true'," in out and "'ldapProtocol' => 3," in out and "'mixedAuth' => true," in out

    def test_the_auth_plugins_are_off_by_default(self):
        out = config_php()
        assert "OidcAuth" not in out and "'auth'" not in out

    def test_oidc_takes_the_documented_names_and_json(self):
        out = config_php("env.OIDC_ENABLE=true", "env.OIDC_PROVIDER_URL=https://idp", "env.OIDC_SCOPES=profile\\,email")
        assert "'auth' => array('OidcAuth.Oidc')," in out
        assert "'provider_url' => 'https://idp'," in out
        assert "'scopes' => array('profile', 'email')," in out
        out = config_php("env.OIDC_ENABLE=true", 'env.OIDC_SCOPES=["openid"\\,"email"]')
        assert "'scopes' => array('openid', 'email')," in out

    def test_a_derived_name_wins_over_its_alias(self):
        out = config_php("env.OIDC_ENABLE=true", "env.OIDC_CLIENT_ID=short", "env.OIDCAUTH_CLIENT_ID=derived")
        assert "'client_id' => 'derived'," in out

    def test_two_plugins_share_security_auth(self):
        out = config_php("env.OIDC_ENABLE=true", "env.LDAPAUTH_ENABLE=true")
        assert "'auth' => array('OidcAuth.Oidc', 'LdapAuth.Ldap')," in out

    def test_custom_auth_aliases_reach_the_env_layer(self):
        env = rendered("env.CUSTOM_AUTH_HEADER=X-User", "env.OIDC_LOGOUT_URL=https://idp/logout")
        assert env["MISP_SETTING_Plugin__CustomAuth_header"] == "X-User"
        assert env["MISP_SETTING_Plugin__CustomAuth_custom_logout"] == "https://idp/logout"
