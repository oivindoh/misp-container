"""Unit tests for admin helpers: every value reaches SQL as a parameter."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import admin

UUID = "550e8400-e29b-41d4-a716-446655440000"


class FakeDb:
    """The organisation queries of _configure_admin_org against a small state."""

    def __init__(self, by_uuid=None, generated=None, user_org=1, host_org=None):
        self.by_uuid, self.generated, self.user_org, self.host_org = by_uuid, generated, user_org, host_org
        self.executed = []

    def dict_query(self, sql, params=()):
        if "WHERE uuid" in sql:
            return [{"id": self.by_uuid}] if self.by_uuid else []
        if "type = 'ADMIN'" in sql:
            return [{"id": self.generated}] if self.generated else []
        if "FROM users WHERE id = 1" in sql:
            return [{"org_id": self.user_org}]
        raise AssertionError(sql)

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if sql.startswith("INSERT INTO organisations"):
            self.by_uuid = 9
        return 1

    def run(self, admin_org, org_uuid=""):
        with patch.dict(os.environ, {"ADMIN_ORG_UUID": org_uuid}), \
                patch("misp_container.admin.db.dict_query", self.dict_query), \
                patch("misp_container.admin.db.execute", self.execute), \
                patch("misp_container.admin.db.get_system_setting", return_value=self.host_org), \
                patch("misp_container.admin.cake.set_setting") as set_setting:
            admin._configure_admin_org(admin_org)
        return set_setting


class TestConfigureAdminOrg:
    def test_a_name_with_an_apostrophe_travels_as_a_parameter(self):
        db = FakeDb(by_uuid=7)
        set_setting = db.run("O'Brien's CERT", UUID)
        assign, rename = db.executed
        assert assign[1] == (7,)
        assert "O'Brien" not in rename[0] and rename[1] == ("O'Brien's CERT", 7, "O'Brien's CERT")
        set_setting.assert_called_once_with("MISP.host_org_id", 7)

    def test_a_new_uuid_creates_the_org(self):
        db = FakeDb(user_org=9)
        db.run("CERT", UUID)
        insert = db.executed[0]
        assert insert[0].startswith("INSERT INTO organisations") and insert[1] == ("CERT", UUID)
        assert len(db.executed) == 2  # insert, rename; the admin is in the org already

    def test_the_org_misp_generated_takes_the_uuid(self):
        # MISP generated it for its first user, named after MISP.org
        db = FakeDb(generated=1)
        set_setting = db.run("CERT", UUID)
        adopt = db.executed[0]
        assert adopt[0].startswith("UPDATE organisations SET uuid") and adopt[1] == (UUID, 1)
        assert not any(sql.startswith("INSERT") for sql, _ in db.executed)
        set_setting.assert_called_once_with("MISP.host_org_id", 1)

    def test_without_a_uuid_the_admins_org_is_renamed_and_hosts(self):
        db = FakeDb(user_org=4)
        set_setting = db.run("CERT")
        assert db.executed == [(db.executed[0][0], ("CERT", 4, "CERT"))]
        set_setting.assert_called_once_with("MISP.host_org_id", 4)

    def test_the_host_org_is_set_only_when_it_changes(self):
        # Each set is a PHP process and a log row in MISP
        db = FakeDb(user_org=4, host_org=4)
        db.run("CERT").assert_not_called()

    def test_the_default_name_changes_nothing(self):
        db = FakeDb(host_org=1)
        db.run("ORGNAME").assert_not_called()
        assert db.executed == []


class TestSetAdminAuthkey:
    KEY = "abcd" + "x" * 32 + "wxyz"

    @staticmethod
    def stored(key):
        import bcrypt
        return bcrypt.hashpw(key.encode(), bcrypt.gensalt(4)).decode().replace("$2b$", "$2y$", 1)

    @patch("misp_container.admin.cake.user_change_authkey")
    @patch("misp_container.admin.db.dict_query", return_value=[])
    def test_the_lookup_uses_the_first_and_last_four_characters(self, query, change):
        admin._set_admin_authkey("admin@x", self.KEY)
        assert query.call_args[0][1] == (1, "abcd", "wxyz")
        change.assert_called_once_with("admin@x", self.KEY)

    @patch("misp_container.admin.cake.user_change_authkey")
    @patch("misp_container.admin.db.dict_query")
    def test_a_key_the_table_holds_is_kept(self, query, change):
        query.return_value = [{"authkey": self.stored(self.KEY)}]
        admin._set_admin_authkey("admin@x", self.KEY)
        change.assert_not_called()

    @patch("misp_container.admin.cake.user_change_authkey")
    @patch("misp_container.admin.db.dict_query")
    def test_another_key_with_the_same_ends_is_replaced(self, query, change):
        query.return_value = [{"authkey": self.stored("abcd" + "y" * 32 + "wxyz")}]
        admin._set_admin_authkey("admin@x", self.KEY)
        change.assert_called_once_with("admin@x", self.KEY)


class TestHasPassword:
    @patch("misp_container.admin.db.dict_query")
    def test_the_stored_hash_verifies_the_password(self, query):
        query.return_value = [{"password": TestSetAdminAuthkey.stored("Br3ak-Glass#2026")}]
        assert admin.has_password(10, "Br3ak-Glass#2026")
        assert query.call_args[0][1] == (10,)

    @patch("misp_container.admin.db.dict_query")
    def test_another_password_does_not_verify(self, query):
        query.return_value = [{"password": TestSetAdminAuthkey.stored("Other-Pass#2026")}]
        assert not admin.has_password(10, "Br3ak-Glass#2026")

    @patch("misp_container.admin.db.dict_query", return_value=[{"password": "5f4dcc3b5aa765d61d8327deb882cf99"}])
    def test_a_hash_that_is_no_bcrypt_does_not_verify(self, query):
        assert not admin.has_password(10, "password")
