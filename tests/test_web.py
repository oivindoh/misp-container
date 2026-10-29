"""Unit tests for the web role (misp_container.web): the session path and the PHP-FPM pool."""

import os
import sys
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import web


class TestSessionSavePath:
    def test_the_password_travels_quoted(self):
        # phpredis parses the part after ? as a query string
        password = "redis'\"$\\@:/?#&%-pw"
        with patch.dict(os.environ, {"MISP_REDIS_HOST": "redis", "MISP_REDIS_PORT": "6379",
                                     "MISP_REDIS_PASSWORD": password}):
            web.set_session_save_path()
            path = os.environ["SESSION_SAVE_PATH"]
        assert path.startswith("tcp://redis:6379?auth=")
        assert urllib.parse.unquote(path.split("?auth=", 1)[1]) == password
        assert not set("?#&") & set(path.split("?auth=", 1)[1])

    def test_a_tls_host_keeps_its_scheme_and_no_password_no_auth(self):
        with patch.dict(os.environ, {"MISP_REDIS_HOST": "tls://cache", "MISP_REDIS_PORT": "6380",
                                     "MISP_REDIS_PASSWORD": ""}):
            web.set_session_save_path()
            assert os.environ["SESSION_SAVE_PATH"] == "tls://cache:6380"


class TestFpmPool:
    def test_each_setting_of_the_env_layer_reaches_the_workers(self, tmp_path, monkeypatch):
        # clear_env = yes keeps every other variable, the secrets included, out of PHP
        pool = tmp_path / "pool.conf"
        pool.write_text("[www]\nclear_env = yes\n")
        monkeypatch.setattr(web, "FPM_POOL", str(pool))
        monkeypatch.setattr(web, "FPM_POOL_RENDERED", str(tmp_path / "rendered.conf"))
        with patch.dict(os.environ, {"MISP_SETTING_MISP__title_text": "x", "MISP_SETTING_debug": "0",
                                     "DB_PASSWORD": "secret"}):
            web.render_fpm_pool()
        lines = (tmp_path / "rendered.conf").read_text().splitlines()
        assert lines[:2] == ["[www]", "clear_env = yes"]
        assert "env[MISP_SETTING_MISP__title_text] = $MISP_SETTING_MISP__title_text" in lines
        assert "env[MISP_SETTING_debug] = $MISP_SETTING_debug" in lines
        assert not [line for line in lines if "DB_PASSWORD" in line]
