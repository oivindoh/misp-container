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
    "MYSQL_PASSWORD": "db-pass",
    "MYSQL_ROOT_PASSWORD": "root-pass",
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
        ("SECURITY_SALT", "0" * 64),
        ("ADMIN_PASSWORD", "change-me"),
        ("MYSQL_PASSWORD", "REPLACE-WITH-REAL-PASSWORD"),
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
        """ChangeMe-... is the documented Compose demo password, not a placeholder."""
        with patch.dict(os.environ, {**GOOD, "ADMIN_PASSWORD": "ChangeMe-Str0ng!Pass#2026"}):
            configure.check_identity()
