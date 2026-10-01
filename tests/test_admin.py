"""Unit tests for admin helpers: every value reaches SQL as a parameter, on either engine."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import admin

UUID = "550e8400-e29b-41d4-a716-446655440000"


class TestConfigureAdminOrg:
    @patch("misp_container.admin.cake.set_setting")
    @patch("misp_container.admin.db.execute")
    @patch("misp_container.admin.db.dict_query")
    def test_a_name_with_an_apostrophe_travels_as_a_parameter(self, query, execute, set_setting):
        query.side_effect = [[{"id": 7}], [{"org_id": 1}]]
        with patch.dict(os.environ, {"ADMIN_ORG_UUID": UUID}):
            admin._configure_admin_org("O'Brien's CERT")
        rename, assign = execute.call_args_list
        assert "O'Brien" not in rename[0][0]
        assert rename[0][1] == ("O'Brien's CERT", 7, "O'Brien's CERT")
        assert assign[0][1] == (7,)
        set_setting.assert_called_once_with("MISP.host_org_id", 7)

    @patch("misp_container.admin.cake.set_setting")
    @patch("misp_container.admin.db.execute")
    @patch("misp_container.admin.db.dict_query")
    def test_a_new_uuid_creates_the_org(self, query, execute, set_setting):
        query.side_effect = [[], [{"id": 9}], [{"org_id": 9}]]
        with patch.dict(os.environ, {"ADMIN_ORG_UUID": UUID}):
            admin._configure_admin_org("CERT")
        insert = execute.call_args_list[0][0]
        assert insert[0].startswith("INSERT INTO organisations")
        assert insert[1] == ("CERT", UUID)
        assert len(execute.call_args_list) == 2  # insert, rename; the admin is in the org already

    @patch("misp_container.admin.db.execute")
    @patch("misp_container.admin.db.dict_query")
    def test_without_a_uuid_the_first_org_is_renamed(self, query, execute):
        with patch.dict(os.environ, {"ADMIN_ORG_UUID": ""}):
            admin._configure_admin_org("CERT")
        query.assert_not_called()
        assert execute.call_args[0][1] == ("CERT", "CERT")

    @patch("misp_container.admin.db.execute")
    @patch("misp_container.admin.db.dict_query")
    def test_the_default_name_changes_nothing(self, query, execute):
        with patch.dict(os.environ, {"ADMIN_ORG_UUID": ""}):
            admin._configure_admin_org("ORGNAME")
        execute.assert_not_called()


class TestSetAdminAuthkey:
    KEY = "abcd" + "x" * 32 + "wxyz"

    @patch("misp_container.admin.cake.user_change_authkey")
    @patch("misp_container.admin.db.dict_query", return_value=[{"n": 0}])
    def test_the_lookup_uses_the_first_and_last_four_characters(self, query, change):
        admin._set_admin_authkey("admin@x", self.KEY)
        assert query.call_args[0][1] == ("abcd", "wxyz")
        change.assert_called_once_with("admin@x", self.KEY)

    @patch("misp_container.admin.cake.user_change_authkey")
    @patch("misp_container.admin.db.dict_query", return_value=[{"n": 1}])
    def test_a_key_the_table_holds_is_kept(self, query, change):
        admin._set_admin_authkey("admin@x", self.KEY)
        change.assert_not_called()
