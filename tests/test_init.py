"""Unit tests for init container logic (file operations, no containers needed)."""

import os
from unittest.mock import patch

import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.init import _generate_email_config, database_fields, write_database_php


class TestGenerateDatabaseConfig:
    """database.php from environment variables."""

    def render(self, tmp_path, env_vars):
        with patch.dict(os.environ, env_vars, clear=False), patch("misp_container.init.MISP_CONFIG", str(tmp_path)):
            write_database_php(database_fields())
        return (tmp_path / "database.php").read_text()

    def test_generates_valid_php(self, tmp_path):
        """Every connection parameter appears in the generated PHP."""
        content = self.render(tmp_path, {"DB_HOST": "db-host", "DB_USER": "dbuser", "DB_PORT": "5433",
                                         "DB_PASSWORD": "secret", "DB_NAME": "testdb"})
        assert "'datasource' => 'Database/PostgresObserverExtended'" in content
        assert "'host' => 'db-host'" in content
        assert "'login' => 'dbuser'" in content
        assert "'port' => 5433" in content
        assert "'password' => 'secret'" in content
        assert "'database' => 'testdb'" in content

    def test_an_empty_port_takes_postgres_default(self, tmp_path):
        assert "'port' => 5432," in self.render(tmp_path, {"DB_PORT": ""})

    def test_a_password_with_a_quote(self, tmp_path):
        assert "'password' => 'pa\\'ss\\\\word'," in self.render(tmp_path, {"DB_PASSWORD": "pa'ss\\word"})

    @pytest.mark.parametrize("tls, sslmode", [
        ({}, "prefer"),
        ({"DB_SSLMODE": "verify-full"}, "verify-full;sslrootcert=system"),
        ({"DB_SSLMODE": "verify-ca", "DB_SSLROOTCERT": "/etc/misp-docker/db-ca/ca.crt"},
         "verify-ca;sslrootcert=/etc/misp-docker/db-ca/ca.crt"),
    ])
    def test_sslmode_follows_db_sslmode(self, tmp_path, tls, sslmode):
        content = self.render(tmp_path, {"DB_SSLMODE": "", "DB_SSLROOTCERT": "", **tls})
        assert f"'sslmode' => '{sslmode}'," in content


class TestGenerateEmailConfig:
    """Generate email.php from environment variables."""

    def test_generates_valid_php(self, tmp_path):
        """SMTP host, port, and sender email appear in the generated PHP."""
        env_vars = {
            "MISP_EMAIL": "misp@example.com",
            "SMTP_FQDN": "smtp.example.com",
            "SMTP_PORT": "587",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            _generate_email_config(tmp_path)
        content = (tmp_path / "email.php").read_text()
        assert "smtp.example.com" in content
        assert "587" in content
        assert "misp@example.com" in content

    def test_an_authenticated_relay_gets_the_login_and_starttls(self, tmp_path):
        env_vars = {"MISP_EMAIL": "m@x", "SMTP_FQDN": "smtp.office365.com", "SMTP_PORT": "587",
                    "SMTP_USERNAME": "misp@example.org", "SMTP_PASSWORD": "it's", "SMTP_TLS": "true"}
        with patch.dict(os.environ, env_vars):
            _generate_email_config(tmp_path)
        content = (tmp_path / "email.php").read_text()
        assert "'username'      => 'misp@example.org'," in content
        assert "'password'      => 'it\\'s'," in content
        assert "'tls'           => true," in content

    def test_without_a_user_name_no_login(self, tmp_path):
        with patch.dict(os.environ, {"MISP_EMAIL": "m@x", "SMTP_FQDN": "mail", "SMTP_USERNAME": "",
                                     "SMTP_PASSWORD": "x", "SMTP_TLS": ""}):
            _generate_email_config(tmp_path)
        content = (tmp_path / "email.php").read_text()
        assert "username" not in content and "'tls'           => false," in content

    def test_a_port_that_is_no_number_falls_back(self, tmp_path):
        with patch.dict(os.environ, {"MISP_EMAIL": "m@x", "SMTP_FQDN": "smtp", "SMTP_PORT": ""}):
            _generate_email_config(tmp_path)
        assert "'port'          => 25," in (tmp_path / "email.php").read_text()


class TestCheckWritable:
    """The entrypoints refuse to start when the attachments directory is read-only."""

    def test_writable_dir_passes(self, tmp_path):
        from misp_container.init import check_writable
        check_writable(str(tmp_path), "attachments")
        assert not (tmp_path / ".write-check").exists()

    def test_missing_dir_exits(self, tmp_path):
        from misp_container.init import check_writable
        with pytest.raises(SystemExit):
            check_writable(str(tmp_path / "missing"), "attachments")


class TestPopulateGnupg:
    """Import of the instance key from the misp-gnupg Secret."""

    def _env(self, tmp_path):
        return {
            "MISP_SETTING_GnuPG__homedir": str(tmp_path / "gnupg"),
            "GNUPG_BINARY": "gpg",
        }

    def test_no_key_is_noop(self, tmp_path):
        from misp_container.init import populate_gnupg
        with patch.dict(os.environ, self._env(tmp_path)), patch("misp_container.init.GNUPG_KEY_FILE", str(tmp_path / "absent.asc")), \
                patch("misp_container.init.subprocess.run") as run:
            populate_gnupg()
        run.assert_not_called()

    @staticmethod
    def _gpg(listings):
        """A fake gpg: --show-keys answers from listings, by fingerprint."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            result = type("R", (), {})()
            fprs = listings["file"] if "--show-keys" in cmd else []
            result.stdout = "".join(f"sec:u:3072:1:AAAA::::::::::\nfpr:::::::::{fpr}:\n" for fpr in fprs)
            return result
        return calls, fake_run

    def test_imports_and_trusts_key(self, tmp_path):
        from misp_container.init import populate_gnupg
        key = tmp_path / "private.asc"
        key.write_text("key")
        calls, fake_run = self._gpg({"file": ["ABCDEF0123456789"]})
        with patch.dict(os.environ, self._env(tmp_path)), patch("misp_container.init.GNUPG_KEY_FILE", str(key)), \
                patch("misp_container.init.subprocess.run", side_effect=fake_run):
            populate_gnupg()

        assert (tmp_path / "gnupg").is_dir()
        assert "--import" in calls[0][0] and str(key) in calls[0][0]
        assert "--show-keys" in calls[1][0] and str(key) in calls[1][0]
        assert "--import-ownertrust" in calls[2][0]
        assert calls[2][1]["input"] == "ABCDEF0123456789:6:\n"
        for cmd, _ in calls:
            assert cmd[:4] == ["gpg", "--batch", "--homedir", str(tmp_path / "gnupg")]


class TestPrepareConfig:
    """app/Config rendering from the image defaults and env."""

    def test_renders_all_files_and_adds_the_log_block_to_bootstrap(self, tmp_path, monkeypatch):
        from misp_container import init as init_mod
        defaults = tmp_path / "defaults"
        defaults.mkdir()
        (defaults / "core.default.php").write_text("<?php // core")
        (defaults / "routes.php").write_text("<?php // routes")
        upstream = ("<?php\nif (empty(Configure::read('SimpleBackgroundJobs.enabled'))) {\n"
                    "\tCakePlugin::loadAll(array('CakeResque' => array('bootstrap' => true)));\n}\n")
        (defaults / "bootstrap.default.php").write_text(upstream)
        config_dir = tmp_path / "Config"
        monkeypatch.setattr(init_mod, "MISP_CONFIG", str(config_dir))
        monkeypatch.setattr(init_mod, "CONFIG_DEFAULTS", str(defaults))
        env_vars = {"DB_HOST": "db", "DB_USER": "u",
                    "DB_PORT": "3306", "DB_PASSWORD": "p", "DB_NAME": "misp", "DB_SSLMODE": "prefer",
                    "MISP_EMAIL": "m@x", "SMTP_FQDN": "smtp", "SMTP_PORT": "25",
                    "MISP_CONFIG_MISP__redis_host": "redis", "MISP_CONFIG_MISP__system_setting_db": "true"}
        with patch.dict(os.environ, env_vars):
            init_mod.prepare_config()
        assert (config_dir / "core.php").read_text() == "<?php // core"
        assert (config_dir / "routes.php").exists()
        bootstrap = (config_dir / "bootstrap.php").read_text()
        # MISP's own bootstrap stays as it is; scripts/check_upstream.py checks what it loads
        assert bootstrap.startswith(upstream)
        assert "'redis_host' => 'redis'" in (config_dir / "config.php").read_text()
        assert "'system_setting_db' => true" in (config_dir / "config.php").read_text()
        assert "'host' => 'db'" in (config_dir / "database.php").read_text()
        assert "'host'          => 'smtp'" in (config_dir / "email.php").read_text()
        # MISP's log goes to stderr, text unless LOG_FORMAT says json
        assert "CakeLog::config('container', array('engine' => 'ContainerLog'));" in bootstrap
        assert "' [misp] '" in bootstrap and "json_encode" not in bootstrap


class TestRenderLogBlock:
    """The CakeLog block in bootstrap.php follows LOG_FORMAT on every start."""

    BASE = "<?php\nCakeLog::config('debug', array('engine' => 'FileLog'));\n"

    def test_json_block_drops_the_file_engines(self):
        from misp_container.init import render_log_block
        out = render_log_block(self.BASE, "json")
        assert "json_encode" in out and "'context' => 'misp'" in out
        assert out.index("CakeLog::drop('debug');") > out.index("CakeLog::config('debug'")
        assert "file_put_contents('php://stderr'" in out

    def test_shells_find_their_stream_names_taken(self):
        from misp_container.init import render_log_block
        out = render_log_block(self.BASE, "json")
        assert "CakeLog::config('stdout', array('engine' => 'ContainerLog', 'types' => array('none')));" in out
        assert "CakeLog::config('stderr', array('engine' => 'ContainerLog', 'types' => array('none')));" in out

    def test_format_change_replaces_the_block(self):
        from misp_container.init import render_log_block, LOG_BLOCK_START, LOG_BLOCK_END
        out = render_log_block(render_log_block(self.BASE, "json"), "text")
        assert out.count(LOG_BLOCK_START) == 1 and out.count(LOG_BLOCK_END) == 1
        assert "json_encode" not in out and "' [misp] '" in out

    def test_second_render_changes_nothing(self):
        from misp_container.init import render_log_block
        once = render_log_block(self.BASE, "json")
        assert render_log_block(once, "json") == once

    def test_unknown_format_is_text(self):
        from misp_container.init import render_log_block
        assert "' [misp] '" in render_log_block(self.BASE, "yaml")

    def test_files_from_an_older_image_are_replaced(self, tmp_path, monkeypatch):
        from misp_container import init as init_mod
        defaults = tmp_path / "defaults"
        defaults.mkdir()
        (defaults / "core.default.php").write_text("<?php // new core")
        (defaults / "routes.php").write_text("<?php // new routes")
        (defaults / "bootstrap.default.php").write_text("<?php // new bootstrap\n")
        config_dir = tmp_path / "Config"
        config_dir.mkdir()
        for name in ("core.php", "routes.php", "bootstrap.php"):
            (config_dir / name).write_text("<?php // from the last release")
        monkeypatch.setattr(init_mod, "MISP_CONFIG", str(config_dir))
        monkeypatch.setattr(init_mod, "CONFIG_DEFAULTS", str(defaults))
        with patch.dict(os.environ, {"DB_PORT": "3306", "SMTP_PORT": "25"}):
            init_mod.prepare_config()
        assert (config_dir / "core.php").read_text() == "<?php // new core"
        assert (config_dir / "routes.php").read_text() == "<?php // new routes"
        assert (config_dir / "bootstrap.php").read_text().startswith("<?php // new bootstrap")
        assert not [p.name for p in config_dir.iterdir() if p.name.startswith(".")]


class TestPopulateCerts:
    """Server certificates from the optional Secret mount."""

    def test_copies_files_into_certs_dir(self, tmp_path):
        from misp_container.init import populate_certs
        src = tmp_path / "secret"
        src.mkdir()
        (src / "3.pem").write_text("cert")
        (src / "..data").mkdir()
        dst = tmp_path / "certs"
        with patch("misp_container.init.CERTS_DIR", str(src)), patch("misp_container.init.MISP_CERTS", str(dst)):
            populate_certs()
        assert (dst / "3.pem").read_text() == "cert"
        assert not (dst / "..data").exists()

    def test_no_secret_is_noop(self, tmp_path):
        from misp_container.init import populate_certs
        with patch("misp_container.init.CERTS_DIR", str(tmp_path / "absent")), patch("misp_container.init.MISP_CERTS", str(tmp_path / "certs")):
            populate_certs()
        assert not (tmp_path / "certs").exists()


class TestPurgeModelCache:
    def test_the_cakephp_schema_caches_go_and_the_rest_stays(self, tmp_path, monkeypatch):
        from misp_container import init
        monkeypatch.setattr(init, "MISP_TMP", str(tmp_path))
        for name in ("cache/models/myapp_cake_model_default_misp_attributes", "cache/persistent/myapp_cake_core_file_map",
                     "cache/views/page", "logs/error.log"):
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text("cached")
        init.purge_model_cache()
        assert not any((tmp_path / "cache/models").iterdir())
        assert not any((tmp_path / "cache/persistent").iterdir())
        assert (tmp_path / "cache/views/page").exists() and (tmp_path / "logs/error.log").exists()
