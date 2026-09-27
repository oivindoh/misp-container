"""Unit tests for config.php rendering from settings.yaml."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.config import (
    SettingSpec,
    config_php_specs,
    php_literal,
    render_config_php,
)
from misp_container.init import _generate_database_config, _generate_email_config


class TestPhpLiteral:
    def test_types(self):
        assert php_literal(True) == "true"
        assert php_literal(False) == "false"
        assert php_literal(6379) == "6379"
        assert php_literal("redis") == "'redis'"

    def test_escapes_quotes_and_backslashes(self):
        assert php_literal("it's") == "'it\\'s'"
        assert php_literal("a\\b") == "'a\\\\b'"


class TestSettingSpecKind:
    def test_kind_from_yaml_type(self):
        assert SettingSpec.from_dict("A.b", {"value": True}).kind == "bool"
        assert SettingSpec.from_dict("A.b", {"value": 13}).kind == "int"
        assert SettingSpec.from_dict("A.b", {"value": "x"}).kind == "str"

    def test_env_override_is_cast_to_yaml_type(self):
        spec = SettingSpec.from_dict("Plugin.ZeroMQ_enable", {"value": False})
        with patch.dict(os.environ, {"PLUGIN_ZEROMQ_ENABLE": "true"}):
            assert spec.typed_value is True
        with patch.dict(os.environ, {"PLUGIN_ZEROMQ_ENABLE": "false"}):
            assert spec.typed_value is False
        port = SettingSpec.from_dict("MISP.redis_port", {"value": 6379})
        with patch.dict(os.environ, {"MISP_REDIS_PORT": "6380"}):
            assert port.typed_value == 6380


class TestRenderConfigPhp:
    def _specs(self):
        return [
            SettingSpec.from_dict("MISP.redis_host", {"value": "redis"}),
            SettingSpec.from_dict("MISP.redis_port", {"value": 6379}),
            SettingSpec.from_dict("MISP.system_setting_db", {"value": True}),
            SettingSpec.from_dict("Plugin.ZeroMQ_enable", {"value": False}),
            SettingSpec.from_dict("Security.salt", {"value": "", "blank_protection": True}),
            SettingSpec.from_dict("debug", {"value": 0}),
        ]

    def test_sections_and_types(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ("MISP_REDIS_HOST", "MISP_REDIS_PORT", "MISP_SYSTEM_SETTING_DB",
                        "PLUGIN_ZEROMQ_ENABLE", "SECURITY_SALT", "DEBUG"):
                os.environ.pop(key, None)
            out = render_config_php(self._specs())
        assert out.startswith("<?php\n$config = array(")
        assert "    'MISP' => array(" in out
        assert "        'redis_host' => 'redis'," in out
        assert "        'redis_port' => 6379," in out
        assert "        'system_setting_db' => true," in out
        assert "        'ZeroMQ_enable' => false," in out
        assert "    'debug' => 0," in out

    def test_blank_protected_value_is_left_out(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECURITY_SALT", None)
            out = render_config_php(self._specs())
        assert "salt" not in out

    def test_env_value_is_escaped(self):
        with patch.dict(os.environ, {"SECURITY_SALT": "it's a \\ salt"}):
            out = render_config_php(self._specs())
        assert "'salt' => 'it\\'s a \\\\ salt'," in out

    def test_later_spec_overrides_earlier(self):
        specs = [
            SettingSpec.from_dict("MISP.attachments_dir", {"value": "/var/www/MISP/app/attachments"}),
            SettingSpec.from_dict("MISP.attachments_dir", {"value": "s3://"}),
        ]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISP_ATTACHMENTS_DIR", None)
            out = render_config_php(specs)
        assert out.count("attachments_dir") == 1
        assert "'attachments_dir' => 's3://'," in out


class TestConfigPhpSpecs:
    def _all(self):
        return {
            "minimum_config": [SettingSpec.from_dict("MISP.redis_host", {"value": "redis"})],
            "db_enable": [SettingSpec.from_dict("MISP.system_setting_db", {"value": True})],
            "s3": [SettingSpec.from_dict("MISP.attachments_dir", {"value": "s3://"})],
            "optional": [SettingSpec.from_dict("MISP.enable_themes", {"value": True})],
        }

    def test_bootstrap_groups_only(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLUGIN_S3_BUCKET_NAME", None)
            names = [s.name for s in config_php_specs(self._all())]
        assert names == ["MISP.redis_host", "MISP.system_setting_db"]

    def test_s3_group_when_bucket_set(self):
        with patch.dict(os.environ, {"PLUGIN_S3_BUCKET_NAME": "misp"}):
            names = [s.name for s in config_php_specs(self._all())]
        assert names[-1] == "MISP.attachments_dir"


class TestGeneratedPhpEscaping:
    def test_database_password_with_quote(self, tmp_path):
        env_vars = {"DB_HOST": "db", "DB_USER": "u", "DB_PORT": "3306",
                    "DB_PASSWORD": "pa'ss\\word", "DB_NAME": "misp", "DB_TLS": "false"}
        with patch.dict(os.environ, env_vars):
            _generate_database_config(tmp_path)
        content = (tmp_path / "database.php").read_text()
        assert "'password' => 'pa\\'ss\\\\word'," in content
        assert "'port' => 3306," in content

    def test_email_port_falls_back_when_not_numeric(self, tmp_path):
        env_vars = {"MISP_EMAIL": "m@x", "SMTP_FQDN": "smtp", "SMTP_PORT": ""}
        with patch.dict(os.environ, env_vars):
            _generate_email_config(tmp_path)
        content = (tmp_path / "email.php").read_text()
        assert "'port'          => 25," in content


class TestPluginGroups:
    """Auth plugin config is rendered into config.php only when its switch is on."""

    def _all(self):
        return {
            "minimum_config": [SettingSpec.from_dict("MISP.redis_host", {"value": "redis"})],
            "db_enable": [],
            "oidc": [
                SettingSpec.from_dict("Security.auth", {"value": ["OidcAuth.Oidc"]}),
                SettingSpec.from_dict("OidcAuth.scopes", {"value": ["profile", "email"]}),
                SettingSpec.from_dict("OidcAuth.role_mapper", {"value": {}}),
                SettingSpec.from_dict("OidcAuth.client_id", {"value": "", "blank_protection": True}),
            ],
            "ldap": [SettingSpec.from_dict("Security.auth", {"value": ["LdapAuth.Ldap"]})],
        }

    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OIDC_ENABLE", None); os.environ.pop("LDAPAUTH_ENABLE", None)
            names = [s.name for s in config_php_specs(self._all())]
        assert names == ["MISP.redis_host"]

    def test_oidc_rendered_with_json_env_overrides(self):
        env = {"OIDC_ENABLE": "true", "OIDCAUTH_ROLE_MAPPER": '{"misp-admin": "admin", "misp-user": 3}',
               "OIDCAUTH_SCOPES": "profile,email,groups", "OIDCAUTH_CLIENT_ID": "misp"}
        with patch.dict(os.environ, env):
            os.environ.pop("LDAPAUTH_ENABLE", None)
            out = render_config_php(config_php_specs(self._all()))
        assert "'auth' => array('OidcAuth.Oidc')," in out
        assert "'scopes' => array('profile', 'email', 'groups')," in out
        assert "'role_mapper' => array('misp-admin' => 'admin', 'misp-user' => 3)," in out
        assert "'client_id' => 'misp'," in out

    def test_two_plugins_share_security_auth(self):
        with patch.dict(os.environ, {"OIDC_ENABLE": "true", "LDAPAUTH_ENABLE": "true"}):
            os.environ.pop("OIDCAUTH_CLIENT_ID", None)
            out = render_config_php(config_php_specs(self._all()))
        assert "'auth' => array('OidcAuth.Oidc', 'LdapAuth.Ldap')," in out
        assert "client_id" not in out

    def test_empty_dict_and_list(self):
        assert php_literal({}) == "array()"
        assert php_literal([]) == "array()"
