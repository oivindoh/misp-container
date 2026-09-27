"""Unit tests for init container logic (file operations, no containers needed)."""

import os
from unittest.mock import patch
from pathlib import Path

import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.init import _generate_database_config, _generate_email_config


class TestGenerateDatabaseConfig:
    """Generate database.php from environment variables."""

    def test_generates_valid_php(self, tmp_path):
        """All MySQL connection parameters appear in the generated PHP."""
        env_vars = {
            "DB_HOST": "db-host",
            "DB_USER": "dbuser",
            "DB_PORT": "3307",
            "DB_PASSWORD": "secret",
            "DB_NAME": "testdb",
            "DB_TLS": "false",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            _generate_database_config(tmp_path)
        content = (tmp_path / "database.php").read_text()
        assert "'host' => 'db-host'" in content
        assert "'login' => 'dbuser'" in content
        assert "'port' => 3307" in content
        assert "'password' => 'secret'" in content
        assert "'database' => 'testdb'" in content

    def test_tls_settings(self, tmp_path):
        """TLS CA path is included when MYSQL_TLS=true and file exists."""
        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("cert")
        env_vars = {
            "DB_HOST": "h", "DB_USER": "u", "DB_PORT": "3306",
            "DB_PASSWORD": "p", "DB_NAME": "d",
            "DB_TLS": "true",
            "MYSQL_TLS_CA": str(ca_file),
            "MYSQL_TLS_CERT": "",
            "MYSQL_TLS_KEY": "",
        }
        with patch.dict(os.environ, env_vars, clear=False):
            _generate_database_config(tmp_path)
        content = (tmp_path / "database.php").read_text()
        assert "'ssl_ca'" in content


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
    """Import of the instance key from the mounted Secret."""

    def _env(self, tmp_path, key_file):
        return {
            "GNUPG_KEY_FILE": str(key_file),
            "GNUPG_HOMEDIR": str(tmp_path / "gnupg"),
            "GNUPG_BINARY": "gpg",
        }

    def test_no_key_file_is_noop(self, tmp_path):
        from misp_container.init import populate_gnupg
        with patch.dict(os.environ, self._env(tmp_path, tmp_path / "absent.asc")), \
                patch("misp_container.init.subprocess.run") as run:
            populate_gnupg()
        run.assert_not_called()

    def test_existing_homedir_is_kept(self, tmp_path):
        from misp_container.init import populate_gnupg
        key = tmp_path / "private.asc"
        key.write_text("key")
        homedir = tmp_path / "gnupg"
        homedir.mkdir()
        (homedir / "trustdb.gpg").write_text("")
        with patch.dict(os.environ, self._env(tmp_path, key)), \
                patch("misp_container.init.subprocess.run") as run:
            populate_gnupg()
        run.assert_not_called()

    def test_imports_and_trusts_key(self, tmp_path):
        from misp_container.init import populate_gnupg
        key = tmp_path / "private.asc"
        key.write_text("key")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            result = type("R", (), {})()
            result.stdout = "sec:u:3072:1:AAAA::::::::::\nfpr:::::::::ABCDEF0123456789:\n"
            return result

        with patch.dict(os.environ, self._env(tmp_path, key)), \
                patch("misp_container.init.subprocess.run", side_effect=fake_run):
            populate_gnupg()

        assert (tmp_path / "gnupg").is_dir()
        assert "--import" in calls[0][0] and str(key) in calls[0][0]
        assert "--list-secret-keys" in calls[1][0]
        assert "--import-ownertrust" in calls[2][0]
        assert calls[2][1]["input"] == "ABCDEF0123456789:6:\n"
        for cmd, _ in calls:
            assert cmd[:4] == ["gpg", "--batch", "--homedir", str(tmp_path / "gnupg")]


class TestPrepareConfig:
    """app/Config rendering from the image defaults, settings.yaml and env."""

    def test_renders_all_files_and_patches_bootstrap(self, tmp_path, monkeypatch):
        from misp_container import init as init_mod
        defaults = tmp_path / "defaults"
        defaults.mkdir()
        (defaults / "core.default.php").write_text("<?php // core")
        (defaults / "routes.php").write_text("<?php // routes")
        (defaults / "bootstrap.default.php").write_text(
            "<?php\nCakePlugin::load('CakeResque');\nCakePlugin::loadAll(array('CakeResque' => array()));\n")
        settings = tmp_path / "settings.yaml"
        settings.write_text("settings:\n  minimum_config:\n    MISP.redis_host:\n      value: redis\n  db_enable:\n    MISP.system_setting_db:\n      value: true\n")
        config_dir = tmp_path / "Config"
        monkeypatch.setattr(init_mod, "MISP_CONFIG", str(config_dir))
        monkeypatch.setattr("misp_container.config.CONFIG_DIR", str(tmp_path))
        env_vars = {"MISP_CONFIG_DEFAULTS": str(defaults), "DB_HOST": "db", "DB_USER": "u",
                    "DB_PORT": "3306", "DB_PASSWORD": "p", "DB_NAME": "misp", "DB_TLS": "false",
                    "MISP_EMAIL": "m@x", "SMTP_FQDN": "smtp", "SMTP_PORT": "25"}
        with patch.dict(os.environ, env_vars):
            os.environ.pop("MISP_REDIS_HOST", None)
            init_mod.prepare_config()
        assert (config_dir / "core.php").read_text() == "<?php // core"
        assert (config_dir / "routes.php").exists()
        bootstrap = (config_dir / "bootstrap.php").read_text()
        assert "CakeResque" not in bootstrap.split("Detect what auth modules")[0]
        assert "Detect what auth modules" in bootstrap
        assert "'redis_host' => 'redis'" in (config_dir / "config.php").read_text()
        assert "'system_setting_db' => true" in (config_dir / "config.php").read_text()
        assert "'host' => 'db'" in (config_dir / "database.php").read_text()
        assert "'host'          => 'smtp'" in (config_dir / "email.php").read_text()

    def test_existing_core_php_is_kept(self, tmp_path, monkeypatch):
        from misp_container import init as init_mod
        defaults = tmp_path / "defaults"
        defaults.mkdir()
        (defaults / "core.default.php").write_text("<?php // new")
        config_dir = tmp_path / "Config"
        config_dir.mkdir()
        (config_dir / "core.php").write_text("<?php // mine")
        settings = tmp_path / "settings.yaml"
        settings.write_text("settings: {}\n")
        monkeypatch.setattr(init_mod, "MISP_CONFIG", str(config_dir))
        monkeypatch.setattr("misp_container.config.CONFIG_DIR", str(tmp_path))
        with patch.dict(os.environ, {"MISP_CONFIG_DEFAULTS": str(defaults), "DB_PORT": "3306", "SMTP_PORT": "25"}):
            init_mod.prepare_config()
        assert (config_dir / "core.php").read_text() == "<?php // mine"


class TestPopulateCerts:
    """Server certificates from the optional Secret mount."""

    def test_copies_files_into_certs_dir(self, tmp_path):
        from misp_container.init import populate_certs
        src = tmp_path / "secret"
        src.mkdir()
        (src / "3.pem").write_text("cert")
        (src / "..data").mkdir()
        dst = tmp_path / "certs"
        with patch.dict(os.environ, {"MISP_CERTS_SOURCE": str(src), "MISP_CERTS_DIR": str(dst)}):
            populate_certs()
        assert (dst / "3.pem").read_text() == "cert"
        assert not (dst / "..data").exists()

    def test_no_secret_is_noop(self, tmp_path):
        from misp_container.init import populate_certs
        with patch.dict(os.environ, {"MISP_CERTS_SOURCE": str(tmp_path / "absent"), "MISP_CERTS_DIR": str(tmp_path / "certs")}):
            populate_certs()
        assert not (tmp_path / "certs").exists()
