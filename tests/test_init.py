"""Unit tests for init container logic (file operations, no containers needed)."""

import os
from unittest.mock import patch
from pathlib import Path

import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.init import _copy_no_clobber, _make_writable, _generate_database_config, _generate_email_config


class TestCopyNoClobber:
    """Copy files without overwriting existing ones (for user-customizable dirs)."""

    def test_copies_new_files(self, tmp_path):
        """New files are copied to the destination."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "file.txt").write_text("hello")
        _copy_no_clobber(src, dst)
        assert (dst / "file.txt").read_text() == "hello"

    def test_does_not_overwrite_existing(self, tmp_path):
        """Existing files in dst are preserved (not overwritten by src)."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "file.txt").write_text("new")
        (dst / "file.txt").write_text("existing")
        _copy_no_clobber(src, dst)
        assert (dst / "file.txt").read_text() == "existing"

    def test_copies_nested_dirs(self, tmp_path):
        """Nested directory structures are copied recursively."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "sub").mkdir(parents=True)
        dst.mkdir()
        (src / "sub" / "deep.txt").write_text("deep")
        _copy_no_clobber(src, dst)
        assert (dst / "sub" / "deep.txt").read_text() == "deep"


class TestMakeWritable:
    """Ensure file trees are writable (for Docker Compose volume pre-population)."""

    def test_makes_readonly_files_writable(self, tmp_path):
        """Files with 0440 permissions become writable after _make_writable."""
        f = tmp_path / "readonly.txt"
        f.write_text("data")
        f.chmod(0o440)
        _make_writable(str(tmp_path))
        assert os.access(str(f), os.W_OK)

    def test_handles_nonexistent_path(self):
        """Non-existent path does not raise an exception."""
        _make_writable("/nonexistent/path")


class TestGenerateDatabaseConfig:
    """Generate database.php from environment variables."""

    def test_generates_valid_php(self, tmp_path):
        """All MySQL connection parameters appear in the generated PHP."""
        env_vars = {
            "MYSQL_HOST": "db-host",
            "MYSQL_USER": "dbuser",
            "MYSQL_PORT": "3307",
            "MYSQL_PASSWORD": "secret",
            "MYSQL_DATABASE": "testdb",
            "MYSQL_TLS": "false",
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
            "MYSQL_HOST": "h", "MYSQL_USER": "u", "MYSQL_PORT": "3306",
            "MYSQL_PASSWORD": "p", "MYSQL_DATABASE": "d",
            "MYSQL_TLS": "true",
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


class TestCopyTree:
    """Full-sync copy used for app/files directories (no copystat, so no xattrs)."""

    def test_copies_nested_and_overwrites(self, tmp_path):
        from misp_container.init import _copy_tree
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "a.txt").write_text("new")
        (src / "top.txt").write_text("top")
        (dst / "sub").mkdir(parents=True)
        (dst / "sub" / "a.txt").write_text("old")
        _copy_tree(src, dst)
        assert (dst / "sub" / "a.txt").read_text() == "new"
        assert (dst / "top.txt").read_text() == "top"


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
