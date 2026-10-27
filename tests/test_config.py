"""Unit tests for config.php from the MISP_CONFIG_* env (misp_container.config)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.config import config_php_values, php_literal, render_config_php, repository_settings


class TestConfigPhpValues:
    def test_categories_become_sections_of_json_values(self):
        env = {"MISP_CONFIG_MISP__system_setting_db": "true", "MISP_CONFIG_LdapAuth__ldapProtocol": "3",
               "MISP_CONFIG_OidcAuth__scopes": '["profile","email"]', "MISP_CONFIG_OidcAuth__client_id": '"0071"',
               "MISP_SETTING_MISP__title_text": "x", "PATH": "/bin"}
        assert config_php_values(env) == {"MISP": {"system_setting_db": True}, "LdapAuth": {"ldapProtocol": 3},
                                          "OidcAuth": {"scopes": ["profile", "email"], "client_id": "0071"}}

    def test_a_value_a_secret_fills_stays_a_plain_string(self):
        # The chart cannot encode a Secret's value: 0071234, true or [ stay what they are
        env = {"MISP_CONFIG_OidcAuth__client_secret": "0071234", "MISP_CONFIG_LdapAuth__ldapReaderPassword": "true",
               "MISP_PLAIN_CONFIG": "MISP_CONFIG_OidcAuth__client_secret,MISP_CONFIG_LdapAuth__ldapReaderPassword"}
        assert config_php_values(env) == {"OidcAuth": {"client_secret": "0071234"},
                                          "LdapAuth": {"ldapReaderPassword": "true"}}

    def test_a_value_that_is_no_json_stays_a_string(self):
        assert config_php_values({"MISP_CONFIG_LdapAuth__ldapTlsCustomCaCert": "/etc/ca.pem"}) == \
            {"LdapAuth": {"ldapTlsCustomCaCert": "/etc/ca.pem"}}


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
