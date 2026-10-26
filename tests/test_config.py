"""Unit tests for config.php from the MISP_CONFIG_* env (misp_container.config)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.config import config_php_values, env_value, php_literal, render_config_php, repository_settings


class TestEnvValue:
    """The types MISP's EnvSetting gives an env value."""

    def test_booleans_null_and_numbers(self):
        assert [env_value(v) for v in ("true", "FALSE", "null", "6379", "-1", "0.5")] == [True, False, None, 6379, -1, 0.5]

    def test_json(self):
        assert env_value('["OidcAuth.Oidc"]') == ["OidcAuth.Oidc"]
        assert env_value('{"misp-admin": "admin"}') == {"misp-admin": "admin"}

    def test_anything_else_stays_a_string(self):
        assert [env_value(v) for v in ("redis", "", "s3://", "[not json")] == ["redis", "", "s3://", "[not json"]


class TestConfigPhpValues:
    def test_categories_become_sections(self):
        env = {"MISP_CONFIG_MISP__redis_host": "redis", "MISP_CONFIG_MISP__redis_port": "6379",
               "MISP_CONFIG_debug": "0", "MISP_SETTING_MISP__title_text": "x", "PATH": "/bin"}
        assert config_php_values(env) == {"MISP": {"redis_host": "redis", "redis_port": 6379}, "debug": 0}


class TestRepositorySettings:
    def test_the_names_the_chart_lists(self):
        assert repository_settings({"MISP_REPOSITORY_SETTINGS": "MISP.org,GnuPG.sign"}) == {"MISP.org", "GnuPG.sign"}
        assert repository_settings({}) == set()

    def test_and_the_ones_the_env_value_sets_directly(self):
        assert repository_settings({"MISP_SETTING_MISP__welcome_text_top": "Hi"}) == {"MISP.welcome_text_top"}


class TestRenderConfigPhp:
    def test_sections_and_types(self):
        out = render_config_php({"MISP": {"redis_host": "redis", "live": True, "none": None}, "debug": 0})
        assert "'MISP' => array(" in out and "'redis_host' => 'redis'," in out
        assert "'live' => true," in out and "'none' => null," in out and "'debug' => 0," in out

    def test_escapes_quotes_and_backslashes(self):
        assert php_literal("it's a \\path") == "'it\\'s a \\\\path'"

    def test_lists_and_maps(self):
        assert php_literal(["a", 1]) == "array('a', 1)"
        assert php_literal({"misp-admin": "admin"}) == "array('misp-admin' => 'admin')"
        assert php_literal({}) == "array()" and php_literal([]) == "array()"
