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
