"""Unit tests for the configure step's identity and secret checks."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import configure

GOOD = {
    "SECURITY_SALT": "a" * 64,
    "SECURITY_ENCRYPTION_KEY": "",
    "ADMIN_PASSWORD": "Long-Passw0rd!Value",
    "ADMIN_KEY": "",
    "DB_PASSWORD": "db-pass",
    "MISP_REDIS_PASSWORD": "redis-pass",
    "GNUPG_PASSWORD": "gpg-pass",
    "MISP_UUID": "1a4e2c6d-0b3f-4a58-9c1d-7e2f3b4c5d6a",
}


class TestCheckIdentity:
    def test_good_values_pass(self):
        with patch.dict(os.environ, GOOD):
            configure.check_identity()

    @pytest.mark.parametrize("key,value", [
        ("SECURITY_SALT", "override-me-generate-a-real-64-char-hex-salt-with-python3-secret"),
        ("ADMIN_PASSWORD", "change-me"),
        ("SECURITY_ENCRYPTION_KEY", "change-me"),
    ])
    def test_placeholder_secret_refused(self, key, value):
        with patch.dict(os.environ, {**GOOD, key: value}):
            with pytest.raises(SystemExit) as exc:
                configure.check_identity()
        assert exc.value.code == 1

    def test_short_salt_refused(self):
        with patch.dict(os.environ, {**GOOD, "SECURITY_SALT": "short"}):
            with pytest.raises(SystemExit):
                configure.check_identity()

    def test_missing_salt_refused(self):
        with patch.dict(os.environ, {**GOOD, "SECURITY_SALT": ""}):
            with pytest.raises(SystemExit):
                configure.check_identity()

    def test_missing_uuid_refused(self):
        with patch.dict(os.environ, {**GOOD, "MISP_UUID": ""}):
            with pytest.raises(SystemExit):
                configure.check_identity()

    def test_case_matters_for_markers(self):
        """ChangeMe-... is no placeholder: the placeholder markers are lower case."""
        with patch.dict(os.environ, {**GOOD, "ADMIN_PASSWORD": "ChangeMe-Str0ng!Pass#2026"}):
            configure.check_identity()


class TestEncryptionKey:
    def test_a_short_key_is_refused(self):
        # MISP refuses every write of a sensitive value under a key shorter than 32 bytes
        with patch.dict(os.environ, {**GOOD, "SECURITY_ENCRYPTION_KEY": "k" * 16}):
            with pytest.raises(SystemExit):
                configure.check_identity()

    def test_a_32_byte_key_passes(self):
        with patch.dict(os.environ, {**GOOD, "SECURITY_ENCRYPTION_KEY": "k" * 32}):
            configure.check_identity()


class TestRepositoryRows:
    def test_the_rows_of_carried_settings_go(self):
        with patch.dict(os.environ, {"MISP_REPOSITORY_SETTINGS": "MISP.org,GnuPG.sign"}), \
                patch.object(configure.db, "execute", return_value=2) as execute:
            configure.forget_repository_rows()
        sql, params = execute.call_args[0]
        assert "DELETE FROM system_settings" in sql and params == (["GnuPG.sign", "MISP.org"],)

    def test_a_manual_setting_is_reported_with_its_change(self):
        rows = [{"setting": "MISP.welcome_text_top", "changed": "by admin@example.org at 2026-10-26 10:00:00"}]
        with patch.object(configure.db, "manual_settings", return_value=rows), \
                patch.object(configure.log, "warning") as warning:
            configure.report_manual_settings()
        assert warning.call_args[0][1] == "MISP.welcome_text_top"


class TestLayoutCheck:
    def test_a_2x_database_is_refused(self, monkeypatch):
        monkeypatch.setattr(configure.db, "query_ok", lambda sql: True)
        monkeypatch.setattr(configure.db, "has_column", lambda t, c: (t, c) == ("attributes", "value1"))
        with pytest.raises(SystemExit):
            configure.check_layout()

    def test_a_30_database_passes(self, monkeypatch):
        monkeypatch.setattr(configure.db, "query_ok", lambda sql: True)
        monkeypatch.setattr(configure.db, "has_column", lambda t, c: False)
        configure.check_layout()

    def test_an_empty_database_passes(self, monkeypatch):
        monkeypatch.setattr(configure.db, "query_ok", lambda sql: False)
        monkeypatch.setattr(configure.db, "has_column", lambda t, c: pytest.fail("looked for a column"))
        configure.check_layout()
