"""Unit tests for the org sync engine."""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.sync import (
    _normalize, _normalize_tag, _merge_configs, _build_rules, _values_equal,
    _validate_uuid, _expand_env, _expand_env_recursive,
    _apply_roles, _apply_orgs, _apply_tags, _apply_one_tag, _apply_one_user,
    _apply_users, _apply_servers, _apply_taxonomies, _apply_warninglists,
    _apply_sharing_groups, _sync_sg_orgs,
    _disable_unmanaged_users, _disable_unmanaged_servers,
    load_config, apply, MISPState, TAG_DEFAULTS,
)


# ---------------------------------------------------------------------------
# Value comparison (MISP string booleans)
# ---------------------------------------------------------------------------

class TestValuesEqual:
    def test_bool_vs_string_one(self):
        assert _values_equal(True, "1")
        assert _values_equal(False, "0")

    def test_bool_vs_string_true(self):
        assert _values_equal(True, "true")
        assert _values_equal(False, "false")

    def test_bool_vs_bool(self):
        assert _values_equal(True, True)
        assert _values_equal(False, False)

    def test_string_vs_string(self):
        assert _values_equal("hello", "hello")
        assert not _values_equal("hello", "world")

    def test_int_vs_string(self):
        assert _values_equal(42, "42")
        assert not _values_equal(42, "43")


# ---------------------------------------------------------------------------
# Config loading and normalization
# ---------------------------------------------------------------------------

class TestValidateUuid:
    def test_valid_v4_uuid(self):
        result = _validate_uuid("4f1ed2b2-1821-49da-bf2c-b7ab639d9b19")
        assert result == "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19"

    def test_uppercase_normalized(self):
        result = _validate_uuid("4F1ED2B2-1821-49DA-BF2C-B7AB639D9B19")
        assert result == "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19"

    def test_invalid_uuid_rejected(self):
        with pytest.raises(ValueError, match="invalid UUID"):
            _validate_uuid("not-a-uuid")

    def test_all_ones_rejected(self):
        # Not valid v4 (version nibble must be 4, variant bits must be 8/9/a/b)
        with pytest.raises(ValueError, match="invalid UUID"):
            _validate_uuid("11111111-1111-1111-1111-111111111111")

    def test_v1_uuid_rejected(self):
        with pytest.raises(ValueError, match="invalid UUID"):
            _validate_uuid("550e8400-e29b-11d4-a716-446655440000")

    def test_context_in_error(self):
        with pytest.raises(ValueError, match="team 'Bad Team'"):
            _validate_uuid("bad", "team 'Bad Team'")


class TestNormalizeTag:
    def test_string_shorthand(self):
        result = _normalize_tag("my-tag")
        assert result["name"] == "my-tag"
        assert result["colour"] == TAG_DEFAULTS["colour"]
        assert result["exportable"] is True

    def test_dict_with_overrides(self):
        result = _normalize_tag({"name": "tlp:red", "colour": "#ff0000"})
        assert result["name"] == "tlp:red"
        assert result["colour"] == "#ff0000"
        assert result["exportable"] is True


class TestNormalize:
    def test_minimal_config(self):
        raw = {"teams": [{"uuid": "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19", "name": "Test"}]}
        result = _normalize(raw)
        assert len(result["teams"]) == 1
        assert result["teams"][0]["uuid"] == "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19"
        assert result["teams"][0]["name"] == "Test"
        assert result["teams"][0]["nationality"] == "Not specified"

    def test_rejects_invalid_uuid(self):
        raw = {"teams": [{"uuid": "not-a-valid-uuid", "name": "Bad"}]}
        with pytest.raises(ValueError, match="invalid UUID"):
            _normalize(raw)

    def test_user_role_defaults(self):
        raw = {"teams": [{
            "uuid": "cc98c3e7-f415-4199-a805-898be218f68a", "name": "T",
            "users": [{"email": "a@b.com"}],
            "feide_users": [{"email": "f@b.com"}],
            "sync_users": [{"email": "s@b.com"}],
            "service_users": [{"email": "bot@b.com"}],
        }]}
        result = _normalize(raw)
        team = result["teams"][0]
        assert team["users"][0]["role"] == "User"
        assert team["feide_users"][0]["role"] == "User"
        assert team["sync_users"][0]["role"] == "Sync user"
        assert team["service_users"][0]["role"] == "User"

    def test_server_defaults(self):
        raw = {"teams": [{
            "uuid": "cc98c3e7-f415-4199-a805-898be218f68a", "name": "T",
            "servers": [{"name": "S", "url": "https://x.com"}],
        }]}
        result = _normalize(raw)
        server = result["teams"][0]["servers"][0]
        assert server["pull"] is True
        assert server["push"] is False
        assert server["pull_tags"] == []

    def test_tags_normalized(self):
        raw = {"tags": ["simple", {"name": "complex", "colour": "#fff"}]}
        result = _normalize(raw)
        assert result["tags"][0]["name"] == "simple"
        assert result["tags"][1]["colour"] == "#fff"

    def test_empty_config(self):
        result = _normalize({})
        assert result["teams"] == []
        assert result["tags"] == []
        assert result["taxonomies"] == []

    def test_default_role_applies_to_users(self):
        raw = {"teams": [{
            "uuid": "cc98c3e7-f415-4199-a805-898be218f68a", "name": "T",
            "default_role": "Org Admin",
            "users": [{"email": "a@b.com"}],
            "feide_users": [{"email": "f@b.com"}],
            "service_users": [{"email": "bot@b.com"}],
        }]}
        result = _normalize(raw)
        team = result["teams"][0]
        assert team["default_role"] == "Org Admin"
        assert team["users"][0]["role"] == "Org Admin"
        assert team["feide_users"][0]["role"] == "Org Admin"
        assert team["service_users"][0]["role"] == "Org Admin"

    def test_default_role_does_not_override_explicit(self):
        raw = {"teams": [{
            "uuid": "cc98c3e7-f415-4199-a805-898be218f68a", "name": "T",
            "default_role": "Org Admin",
            "users": [{"email": "a@b.com", "role": "Admin"}],
        }]}
        result = _normalize(raw)
        assert result["teams"][0]["users"][0]["role"] == "Admin"

    def test_default_role_not_applied_to_sync_users(self):
        """Sync users always default to 'Sync user' regardless of default_role."""
        raw = {"teams": [{
            "uuid": "cc98c3e7-f415-4199-a805-898be218f68a", "name": "T",
            "default_role": "Org Admin",
            "sync_users": [{"email": "s@b.com"}],
        }]}
        result = _normalize(raw)
        assert result["teams"][0]["sync_users"][0]["role"] == "Sync user"

    def test_roles_in_config(self):
        raw = {"roles": [{"name": "Custom Role", "permission": 2, "perm_tagger": True}]}
        result = _normalize(raw)
        assert len(result["roles"]) == 1
        assert result["roles"][0]["name"] == "Custom Role"

    def test_feide_user_disabled_default(self):
        raw = {"teams": [{"uuid": "cc98c3e7-f415-4199-a805-898be218f68a", "name": "T",
                          "feide_users": [{"email": "f@b.com"}]}]}
        result = _normalize(raw)
        assert result["teams"][0]["feide_users"][0]["disabled"] is False


class TestMergeConfigs:
    def test_local_wins_for_top_level_lists(self):
        local = {"taxonomies": ["tlp"], "tags": ["local-tag"]}
        remote = {"taxonomies": ["admiralty"], "tags": ["remote-tag"]}
        result = _merge_configs(local, remote)
        assert result["taxonomies"] == ["tlp"]
        assert result["tags"] == ["local-tag"]

    def test_remote_fills_missing_keys(self):
        local = {"taxonomies": ["tlp"]}
        remote = {"warninglists": ["Top 1000"]}
        result = _merge_configs(local, remote)
        assert result["taxonomies"] == ["tlp"]
        assert result["warninglists"] == ["Top 1000"]

    def test_teams_merge_by_uuid(self):
        local = {"teams": [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Local Name"}]}
        remote = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Remote Name", "sector": "academic"},
            {"uuid": "cb610191-6a92-4502-b5c9-9ab0b0153881", "name": "Remote Only"},
        ]}
        result = _merge_configs(local, remote)
        assert len(result["teams"]) == 2
        assert result["teams"][0]["name"] == "Local Name"
        assert result["teams"][0]["sector"] == "academic"
        assert result["teams"][1]["name"] == "Remote Only"

    def test_local_user_pinned_to_local_team(self):
        """User in local file stays in local team (allow_external defaults to false)."""
        local = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1",
             "users": [{"email": "alice@x.com"}]},
        ]}
        remote = {"teams": [
            {"uuid": "cb610191-6a92-4502-b5c9-9ab0b0153881", "name": "Team2",
             "users": [{"email": "alice@x.com"}]},
        ]}
        result = _merge_configs(local, remote)
        team1 = next(t for t in result["teams"] if t["name"] == "Team1")
        team2 = next(t for t in result["teams"] if t["name"] == "Team2")
        team1_emails = [u["email"] for u in team1.get("users", [])]
        team2_emails = [u["email"] for u in team2.get("users", [])]
        assert "alice@x.com" in team1_emails
        assert "alice@x.com" not in team2_emails

    def test_allow_external_lets_remote_move_user(self):
        """User with allow_external: true can be moved by external source."""
        local = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1",
             "users": [{"email": "alice@x.com", "allow_external": True, "role": "User"}]},
        ]}
        remote = {"teams": [
            {"uuid": "cb610191-6a92-4502-b5c9-9ab0b0153881", "name": "Team2",
             "users": [{"email": "alice@x.com"}]},
        ]}
        result = _merge_configs(local, remote)
        team1 = next(t for t in result["teams"] if t["name"] == "Team1")
        team2 = next(t for t in result["teams"] if t["name"] == "Team2")
        team1_emails = [u["email"] for u in team1.get("users", [])]
        team2_emails = [u["email"] for u in team2.get("users", [])]
        # External moved alice to Team2
        assert "alice@x.com" not in team1_emails
        assert "alice@x.com" in team2_emails

    def test_allow_external_preserves_local_user_fields(self):
        """When external moves a user, local fields (like role) are preserved."""
        local = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1",
             "users": [{"email": "alice@x.com", "allow_external": True, "role": "Admin"}]},
        ]}
        remote = {"teams": [
            {"uuid": "cb610191-6a92-4502-b5c9-9ab0b0153881", "name": "Team2",
             "users": [{"email": "alice@x.com", "role": "User"}]},
        ]}
        result = _merge_configs(local, remote)
        team2 = next(t for t in result["teams"] if t["name"] == "Team2")
        alice = next(u for u in team2["users"] if u["email"] == "alice@x.com")
        # Local role wins (local fields overlay external)
        assert alice["role"] == "Admin"

    def test_remote_only_user_added(self):
        """Users only in remote are added to the remote's team."""
        local = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1"},
        ]}
        remote = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1",
             "users": [{"email": "remote-only@x.com"}]},
        ]}
        result = _merge_configs(local, remote)
        team1 = result["teams"][0]
        emails = [u["email"] for u in team1.get("users", [])]
        assert "remote-only@x.com" in emails

    def test_allow_external_not_in_remote_stays_local(self):
        """allow_external user not in remote stays in local team."""
        local = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1",
             "users": [{"email": "alice@x.com", "allow_external": True}]},
        ]}
        remote = {"teams": []}
        result = _merge_configs(local, remote)
        team1 = result["teams"][0]
        emails = [u["email"] for u in team1.get("users", [])]
        assert "alice@x.com" in emails

    def test_local_disabled_overrides_remote(self):
        """Local disabled: true wins over remote disabled: false."""
        local = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1",
             "users": [{"email": "alice@x.com", "disabled": True}]},
        ]}
        remote = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1",
             "users": [{"email": "alice@x.com", "disabled": False}]},
        ]}
        result = _merge_configs(local, remote)
        team1 = result["teams"][0]
        alice = next(u for u in team1["users"] if u["email"] == "alice@x.com")
        assert alice["disabled"] is True

    def test_local_disabled_overrides_remote_with_allow_external(self):
        """Even with allow_external, local disabled field wins."""
        local = {"teams": [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Team1",
             "users": [{"email": "alice@x.com", "allow_external": True, "disabled": True}]},
        ]}
        remote = {"teams": [
            {"uuid": "cb610191-6a92-4502-b5c9-9ab0b0153881", "name": "Team2",
             "users": [{"email": "alice@x.com", "disabled": False}]},
        ]}
        result = _merge_configs(local, remote)
        # alice moved to Team2 (allow_external), but disabled stays True (local wins)
        team2 = next(t for t in result["teams"] if t["name"] == "Team2")
        alice = next(u for u in team2["users"] if u["email"] == "alice@x.com")
        assert alice["disabled"] is True

    def test_empty_local_and_remote(self):
        result = _merge_configs({}, {})
        assert result["teams"] == []


class TestLoadConfig:
    def test_load_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("taxonomies:\n  - tlp\nteams:\n  - uuid: cc98c3e7-f415-4199-a805-898be218f68a\n    name: Test\n")
            f.flush()
            try:
                result = load_config(file_path=f.name)
                assert result["taxonomies"] == ["tlp"]
                assert result["teams"][0]["name"] == "Test"
            finally:
                os.unlink(f.name)

    def test_missing_file_returns_empty(self):
        result = load_config(file_path="/nonexistent/path.yaml")
        assert result["teams"] == []


# ---------------------------------------------------------------------------
# Apply roles
# ---------------------------------------------------------------------------

class TestApplyRoles:
    def test_creates_new_role(self):
        client = MagicMock()
        state = MagicMock()
        state.roles = {}

        roles = [{"name": "Custom Role", "permission": 2, "perm_tagger": True}]
        counts = _apply_roles(client, state, roles)

        assert counts["created"] == 1
        path, data = client.post.call_args[0]
        assert "roles/add" in path
        assert data["Role"]["name"] == "Custom Role"
        assert data["Role"]["permission"] == 2
        assert data["Role"]["perm_tagger"] is True

    def test_updates_changed_role(self):
        client = MagicMock()
        state = MagicMock()
        state.roles = {"Existing": {"id": 5, "name": "Existing",
                                     "permission": "1", "perm_tagger": "0"}}

        roles = [{"name": "Existing", "permission": 2, "perm_tagger": True}]
        counts = _apply_roles(client, state, roles)

        assert counts["updated"] == 1
        path, data = client.post.call_args[0]
        assert "roles/edit/5" in path

    def test_skips_unchanged_role(self):
        client = MagicMock()
        state = MagicMock()
        state.roles = {"Existing": {"id": 5, "name": "Existing",
                                     "permission": "2", "perm_tagger": "1"}}

        roles = [{"name": "Existing", "permission": 2, "perm_tagger": True}]
        counts = _apply_roles(client, state, roles)

        assert counts["updated"] == 0
        client.post.assert_not_called()

    def test_skips_role_without_name(self):
        client = MagicMock()
        state = MagicMock()
        state.roles = {}

        roles = [{"permission": 2}]
        counts = _apply_roles(client, state, roles)
        assert counts["created"] == 0
        client.post.assert_not_called()


# ---------------------------------------------------------------------------
# Build rules
# ---------------------------------------------------------------------------

class TestBuildRules:
    def test_pull_rules_use_tag_names(self):
        state = MagicMock()
        rules = _build_rules(["tlp:white", "rpz:blocked"], ["internal:*"],
                             pull=True, state=state)
        assert rules["tags"]["OR"] == ["tlp:white", "rpz:blocked"]
        assert rules["tags"]["NOT"] == ["internal:*"]
        assert "url_params" in rules

    def test_push_rules_use_tag_ids(self):
        state = MagicMock()
        state.tag_id = lambda name: {"tlp:white": 1, "rpz:blocked": 2}.get(name)
        rules = _build_rules(["tlp:white", "rpz:blocked"], [],
                             pull=False, state=state)
        assert rules["tags"]["OR"] == [1, 2]
        assert "url_params" not in rules

    def test_push_rules_skip_unknown_tags(self):
        state = MagicMock()
        state.tag_id = lambda name: None
        rules = _build_rules(["unknown:tag"], [], pull=False, state=state)
        assert rules["tags"]["OR"] == []

    def test_pull_rules_with_excludes(self):
        state = MagicMock()
        rules = _build_rules(["tlp:white"], ["internal:*"],
                             pull=True, state=state)
        assert rules["tags"]["NOT"] == ["internal:*"]

    def test_push_rules_with_excludes(self):
        state = MagicMock()
        state.tag_id = lambda name: {"tlp:white": 1, "internal:*": 5}.get(name)
        rules = _build_rules(["tlp:white"], ["internal:*"],
                             pull=False, state=state)
        assert rules["tags"]["OR"] == [1]
        assert rules["tags"]["NOT"] == [5]

    def test_empty_rules(self):
        state = MagicMock()
        rules = _build_rules([], [], pull=True, state=state)
        assert rules["tags"]["OR"] == []
        assert rules["tags"]["NOT"] == []
        assert rules["orgs"]["OR"] == []


# ---------------------------------------------------------------------------
# Env var expansion
# ---------------------------------------------------------------------------

class TestExpandEnv:
    def test_braced_var(self):
        os.environ["TEST_SYNC_VAR"] = "secret123"
        try:
            assert _expand_env("${TEST_SYNC_VAR}") == "secret123"
        finally:
            del os.environ["TEST_SYNC_VAR"]

    def test_unbraced_var(self):
        os.environ["TEST_SYNC_VAR"] = "val"
        try:
            assert _expand_env("$TEST_SYNC_VAR") == "val"
        finally:
            del os.environ["TEST_SYNC_VAR"]

    def test_missing_var_becomes_empty(self):
        assert _expand_env("${NONEXISTENT_VAR_XYZ}") == ""

    def test_no_expansion_without_dollar(self):
        assert _expand_env("plain text") == "plain text"

    def test_recursive_expansion(self):
        os.environ["TEST_SYNC_TOKEN"] = "my-token"
        try:
            config = {
                "teams": [{
                    "name": "Test",
                    "servers": [{"authkey": "${TEST_SYNC_TOKEN}", "url": "https://x.com"}],
                }],
                "tags": ["${TEST_SYNC_TOKEN}"],
            }
            result = _expand_env_recursive(config)
            assert result["teams"][0]["servers"][0]["authkey"] == "my-token"
            assert result["tags"][0] == "my-token"
        finally:
            del os.environ["TEST_SYNC_TOKEN"]

    def test_load_config_expands_env(self):
        os.environ["TEST_SYNC_KEY"] = "expanded-key"
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write(
                    "teams:\n"
                    "  - uuid: 4f1ed2b2-1821-49da-bf2c-b7ab639d9b19\n"
                    "    name: Test\n"
                    "    servers:\n"
                    "      - name: S\n"
                    "        url: https://x.com\n"
                    "        authkey: ${TEST_SYNC_KEY}\n"
                )
                f.flush()
                result = load_config(file_path=f.name)
                assert result["teams"][0]["servers"][0]["authkey"] == "expanded-key"
                os.unlink(f.name)
        finally:
            del os.environ["TEST_SYNC_KEY"]


# ---------------------------------------------------------------------------
# Apply orgs
# ---------------------------------------------------------------------------

class TestApplyOrgs:
    def test_creates_new_org(self):
        client = MagicMock()
        state = MagicMock()
        state.orgs = {}

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "New Org", "description": "A new org",
                  "sector": "academic", "nationality": "NO", "type": "", "contacts": ""}]
        counts = _apply_orgs(client, state, teams)

        assert counts["created"] == 1
        path, data = client.post.call_args[0]
        assert "organisations/add" in path
        org = data["Organisation"]
        assert org["uuid"] == "ee8e368c-1587-4630-99f7-0b7a3ff2674f"
        assert org["name"] == "New Org"
        assert org["description"] == "A new org"
        assert org["sector"] == "academic"
        assert org["local"] is True

    def test_updates_changed_org(self):
        client = MagicMock()
        state = MagicMock()
        state.orgs = {"ee8e368c-1587-4630-99f7-0b7a3ff2674f": {"id": 1, "name": "Old Name", "description": "",
                               "sector": "", "nationality": "", "type": "", "contacts": ""}}

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "New Name", "description": "Updated",
                  "sector": "", "nationality": "", "type": "", "contacts": ""}]
        counts = _apply_orgs(client, state, teams)

        assert counts["updated"] == 1
        path, data = client.post.call_args[0]
        assert "organisations/edit/1" in path
        assert data["Organisation"]["name"] == "New Name"
        assert data["Organisation"]["description"] == "Updated"

    def test_skips_unchanged_org(self):
        client = MagicMock()
        state = MagicMock()
        state.orgs = {"ee8e368c-1587-4630-99f7-0b7a3ff2674f": {"id": 1, "name": "Same", "description": "",
                               "sector": "", "nationality": "", "type": "", "contacts": ""}}

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Same", "description": "",
                  "sector": "", "nationality": "", "type": "", "contacts": ""}]
        counts = _apply_orgs(client, state, teams)

        assert counts["created"] == 0
        assert counts["updated"] == 0
        client.post.assert_not_called()

    def test_creates_multiple_orgs(self):
        client = MagicMock()
        state = MagicMock()
        state.orgs = {}

        teams = [
            {"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "Org A", "description": "",
             "sector": "", "nationality": "", "type": "", "contacts": ""},
            {"uuid": "cb610191-6a92-4502-b5c9-9ab0b0153881", "name": "Org B", "description": "",
             "sector": "", "nationality": "", "type": "", "contacts": ""},
        ]
        counts = _apply_orgs(client, state, teams)
        assert counts["created"] == 2
        assert client.post.call_count == 2


# ---------------------------------------------------------------------------
# Apply tags (global + org-scoped)
# ---------------------------------------------------------------------------

class TestApplyTags:
    def test_global_and_org_scoped(self):
        client = MagicMock()
        state = MagicMock()
        state.tags = {}
        state.org_id = lambda uuid: {"ee8e368c-1587-4630-99f7-0b7a3ff2674f": 5}.get(uuid.lower() if len(uuid) > 10 else uuid)

        global_tags = [{"name": "global:tag", "colour": "#fff",
                        "exportable": True, "hide_tag": False}]
        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "tags": [
            {"name": "org:tag", "colour": "#f00", "exportable": True, "hide_tag": False}
        ]}]

        counts = _apply_tags(client, state, global_tags, teams)
        assert counts["created"] == 2

        # Check global tag has org_id=0
        first_call = client.post.call_args_list[0]
        assert first_call[0][1]["Tag"]["org_id"] == 0

        # Check org-scoped tag has org_id=5
        second_call = client.post.call_args_list[1]
        assert second_call[0][1]["Tag"]["org_id"] == 5


class TestApplyOneTag:
    def test_creates_new_tag(self):
        client = MagicMock()
        state = MagicMock()
        state.tags = {}

        result = _apply_one_tag(client, state,
                                {"name": "new:tag", "colour": "#fff"}, org_id=0)
        assert result == "created"
        data = client.post.call_args[0][1]["Tag"]
        assert data["name"] == "new:tag"
        assert data["colour"] == "#fff"
        assert data["org_id"] == 0

    def test_creates_org_scoped_tag(self):
        client = MagicMock()
        state = MagicMock()
        state.tags = {}

        _apply_one_tag(client, state,
                       {"name": "scoped:tag", "colour": "#abc"}, org_id=7)
        data = client.post.call_args[0][1]["Tag"]
        assert data["org_id"] == 7

    def test_unchanged_tag(self):
        client = MagicMock()
        state = MagicMock()
        state.tags = {"my:tag": {"id": 1, "colour": "#004daa", "exportable": "1",
                                  "hide_tag": "0", "org_id": "0"}}

        result = _apply_one_tag(client, state,
                                {"name": "my:tag", "colour": "#004daa",
                                 "exportable": True, "hide_tag": False},
                                org_id=0)
        assert result == "unchanged"
        client.post.assert_not_called()

    def test_updates_changed_colour(self):
        client = MagicMock()
        state = MagicMock()
        state.tags = {"my:tag": {"id": 1, "colour": "#old", "exportable": "1",
                                  "hide_tag": "0", "org_id": "0"}}

        result = _apply_one_tag(client, state,
                                {"name": "my:tag", "colour": "#new",
                                 "exportable": True, "hide_tag": False},
                                org_id=0)
        assert result == "updated"
        data = client.post.call_args[0][1]["Tag"]
        assert data["colour"] == "#new"


# ---------------------------------------------------------------------------
# Apply users
# ---------------------------------------------------------------------------

class TestApplyOneUser:
    def test_creates_new_user_with_correct_fields(self):
        client = MagicMock()
        state = MagicMock()
        state.users = {}
        state.role_id = lambda name: 3

        result = _apply_one_user(client, state,
                                 {"email": "new@test.com", "role": "User"},
                                 org_id=1, external_auth=False)
        assert result == "created"
        data = client.post.call_args[0][1]["User"]
        assert data["email"] == "new@test.com"
        assert data["org_id"] == 1
        assert data["role_id"] == 3
        assert data["disabled"] is False
        assert data["termsaccepted"] is True
        assert data["change_pw"] == 0
        assert "password" in data  # random password generated
        assert len(data["password"]) == 64  # hex(32 bytes)

    def test_feide_user_sets_external_auth(self):
        client = MagicMock()
        state = MagicMock()
        state.users = {}
        state.role_id = lambda name: 3

        _apply_one_user(client, state,
                        {"email": "feide@inst.no", "role": "User"},
                        org_id=1, external_auth=True)
        data = client.post.call_args[0][1]["User"]
        assert data["external_auth_required"] is True
        assert data["external_auth_key"] == "feide@inst.no"

    @patch("misp_container.sync._set_user_authkey")
    def test_sync_user_with_explicit_authkey(self, mock_set_key):
        """Sync users can have an explicit authkey (inbound credential for remote operators)."""
        client = MagicMock()
        client.post.return_value = {"User": {"id": 42}}
        state = MagicMock()
        state.users = {}
        state.role_id = lambda name: 4

        result = _apply_one_user(client, state,
                                 {"email": "sync@partner.no", "role": "Sync user",
                                  "authkey": "inbound-key-for-partner-0000000000000000"},
                                 org_id=1, external_auth=False)
        assert result == "created"

        # User created via API, then authkey set via direct DB write
        client.post.assert_called_once()
        mock_set_key.assert_called_once_with(42, "inbound-key-for-partner-0000000000000000")

    @patch("misp_container.sync._set_user_authkey")
    def test_existing_user_authkey_reapplied(self, mock_set_key):
        """Authkey is re-applied on every sync for existing users (can't diff hashed keys)."""
        client = MagicMock()
        state = MagicMock()
        state.users = {"sync@partner.no": {"id": 42, "org_id": "1",
                                            "role_id": "4", "disabled": "0"}}
        state.role_id = lambda name: 4

        result = _apply_one_user(client, state,
                                 {"email": "sync@partner.no", "role": "Sync user",
                                  "authkey": "inbound-key-for-partner-0000000000000000"},
                                 org_id=1, external_auth=False)
        # No field changes, but authkey always re-applied
        assert result == "unchanged"
        mock_set_key.assert_called_once_with(42, "inbound-key-for-partner-0000000000000000")

    def test_skips_unknown_role(self):
        client = MagicMock()
        state = MagicMock()
        state.users = {}
        state.role_id = lambda name: None

        result = _apply_one_user(client, state,
                                 {"email": "x@x.com", "role": "Nonexistent"},
                                 org_id=1, external_auth=False)
        assert result == "unchanged"
        client.post.assert_not_called()

    def test_updates_user_org_change(self):
        client = MagicMock()
        state = MagicMock()
        state.users = {"user@test.com": {"id": 10, "org_id": "1",
                                          "role_id": "3", "disabled": "0"}}
        state.role_id = lambda name: 3

        result = _apply_one_user(client, state,
                                 {"email": "user@test.com", "role": "User"},
                                 org_id=2, external_auth=False)
        assert result == "updated"
        data = client.post.call_args[0][1]["User"]
        assert data["org_id"] == 2

    def test_skips_unchanged_user(self):
        client = MagicMock()
        state = MagicMock()
        state.users = {"user@test.com": {"id": 10, "org_id": "1",
                                          "role_id": "3", "disabled": "0"}}
        state.role_id = lambda name: 3

        result = _apply_one_user(client, state,
                                 {"email": "user@test.com", "role": "User"},
                                 org_id=1, external_auth=False)
        assert result == "unchanged"
        client.post.assert_not_called()

    def test_disables_user_when_requested(self):
        client = MagicMock()
        state = MagicMock()
        state.users = {"user@test.com": {"id": 10, "org_id": "1",
                                          "role_id": "3", "disabled": "0"}}
        state.role_id = lambda name: 3

        result = _apply_one_user(client, state,
                                 {"email": "user@test.com", "role": "User",
                                  "disabled": True},
                                 org_id=1, external_auth=False)
        assert result == "updated"
        data = client.post.call_args[0][1]["User"]
        assert data["disabled"] is True


class TestApplyUsers:
    def test_creates_all_user_types_in_team(self):
        client = MagicMock()
        state = MagicMock()
        state.users = {}
        state.orgs = {"ee8e368c-1587-4630-99f7-0b7a3ff2674f": {"id": 5}}
        state.org_id = lambda uuid: 5
        state.role_id = lambda name: {"User": 3, "Sync user": 4}.get(name)

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "T",
                  "users": [{"email": "regular@x.com", "role": "User"}],
                  "feide_users": [{"email": "feide@x.com", "role": "User"}],
                  "sync_users": [{"email": "sync@x.com", "role": "Sync user"}],
                  "service_users": [{"email": "bot@x.com"}],
                  "servers": [], "tags": []}]

        counts, managed = _apply_users(client, state, teams)
        assert counts["created"] == 4
        assert "regular@x.com" in managed
        assert "feide@x.com" in managed
        assert "sync@x.com" in managed
        assert "bot@x.com" in managed


# ---------------------------------------------------------------------------
# Apply servers
# ---------------------------------------------------------------------------

class TestApplyServers:
    def test_creates_server_with_pull_tags(self):
        client = MagicMock()
        state = MagicMock()
        state.orgs = {"ee8e368c-1587-4630-99f7-0b7a3ff2674f": {"id": 5}}
        state.org_id = lambda uuid: 5
        state.users = {}
        state.servers = {}
        state.tag_id = lambda name: None  # no local tags needed for pull rules

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "T",
                  "sync_users": [], "tags": [],
                  "users": [], "feide_users": [], "service_users": [],
                  "servers": [{
                      "name": "Remote",
                      "url": "https://remote.example.com",
                      "pull": True, "push": False,
                      "internal": False, "self_signed": False,
                      "caching_enabled": False,
                      "pull_tags": ["rpz:blocked_domain", "tlp:white"],
                      "push_tags": [],
                      "pull_tags_exclude": ["internal:*"],
                      "push_tags_exclude": [],
                  }]}]

        counts, managed = _apply_servers(client, state, teams)
        assert counts["created"] == 1
        assert "https://remote.example.com" in managed

        path, data = client.post.call_args[0]
        assert "servers/add" in path
        server = data["Server"]
        assert server["name"] == "Remote"
        assert server["url"] == "https://remote.example.com"
        assert server["pull"] is True
        assert server["push"] is False
        assert server["remote_org_id"] == 5
        assert len(server["authkey"]) == 40  # hex(20)

        # Verify pull rules contain tag names (not IDs)
        pull_rules = json.loads(server["pull_rules"])
        assert pull_rules["tags"]["OR"] == ["rpz:blocked_domain", "tlp:white"]
        assert pull_rules["tags"]["NOT"] == ["internal:*"]
        assert "url_params" in pull_rules

        # Verify push rules are empty
        push_rules = json.loads(server["push_rules"])
        assert push_rules["tags"]["OR"] == []

    def test_creates_server_with_push_tags_resolved_to_ids(self):
        client = MagicMock()
        state = MagicMock()
        state.org_id = lambda uuid: 5
        state.users = {}
        state.servers = {}
        state.tag_id = lambda name: {"tlp:white": 10, "rpz:blocked": 20}.get(name)

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "T",
                  "sync_users": [], "tags": [],
                  "users": [], "feide_users": [], "service_users": [],
                  "servers": [{
                      "name": "Push Server",
                      "url": "https://push.example.com",
                      "pull": False, "push": True,
                      "internal": False, "self_signed": False,
                      "caching_enabled": False,
                      "pull_tags": [], "push_tags": ["tlp:white", "rpz:blocked"],
                      "pull_tags_exclude": [], "push_tags_exclude": [],
                  }]}]

        counts, _ = _apply_servers(client, state, teams)
        assert counts["created"] == 1

        server = client.post.call_args[0][1]["Server"]
        push_rules = json.loads(server["push_rules"])
        assert push_rules["tags"]["OR"] == [10, 20]

    def test_server_uses_config_authkey(self):
        """Server authkey from config (our credential on the remote instance)."""
        client = MagicMock()
        state = MagicMock()
        state.org_id = lambda uuid: 5
        state.servers = {}
        state.users = {}
        state.tag_id = lambda name: None

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "T",
                  "sync_users": [], "users": [], "feide_users": [],
                  "service_users": [], "tags": [],
                  "servers": [{
                      "name": "Remote", "url": "https://remote.com",
                      "authkey": "key-from-remote-operator-abcdef123456789",
                      "pull": True, "push": False,
                      "internal": False, "self_signed": False, "caching_enabled": False,
                      "pull_tags": [], "push_tags": [],
                      "pull_tags_exclude": [], "push_tags_exclude": [],
                  }]}]

        _apply_servers(client, state, teams)
        server = client.post.call_args[0][1]["Server"]
        assert server["authkey"] == "key-from-remote-operator-abcdef123456789"

    def test_skips_unchanged_server(self):
        client = MagicMock()
        state = MagicMock()
        state.org_id = lambda uuid: 5
        state.users = {}
        state.tag_id = lambda name: None
        state.servers = {"https://remote.com": {
            "id": 1, "name": "Remote", "internal": False,
            "push": False, "pull": True, "self_signed": False,
            "caching_enabled": False, "remote_org_id": 5,
            "pull_rules": json.dumps({"tags": {"OR": [], "NOT": []},
                                       "orgs": {"OR": [], "NOT": []},
                                       "type_attributes": {"NOT": []},
                                       "type_objects": {"NOT": []},
                                       "url_params": ""}),
            "push_rules": json.dumps({"tags": {"OR": [], "NOT": []},
                                       "orgs": {"OR": [], "NOT": []}}),
            "authkey": "x" * 40,
        }}

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "T",
                  "sync_users": [], "users": [], "feide_users": [],
                  "service_users": [], "tags": [],
                  "servers": [{
                      "name": "Remote", "url": "https://remote.com",
                      "pull": True, "push": False,
                      "internal": False, "self_signed": False, "caching_enabled": False,
                      "pull_tags": [], "push_tags": [],
                      "pull_tags_exclude": [], "push_tags_exclude": [],
                  }]}]

        counts, _ = _apply_servers(client, state, teams)
        assert counts["created"] == 0
        assert counts["updated"] == 0

    def test_explicit_authkey_from_config_on_create(self):
        """Config authkey should be used on create."""
        client = MagicMock()
        state = MagicMock()
        state.org_id = lambda uuid: 5
        state.users = {}
        state.servers = {}
        state.tag_id = lambda name: None

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "T",
                  "sync_users": [], "users": [], "feide_users": [],
                  "service_users": [], "tags": [],
                  "servers": [{
                      "name": "Remote", "url": "https://remote.com",
                      "authkey": "my-explicit-key-from-config-abcdef1234567890",
                      "pull": True, "push": False,
                      "internal": False, "self_signed": False, "caching_enabled": False,
                      "pull_tags": [], "push_tags": [],
                      "pull_tags_exclude": [], "push_tags_exclude": [],
                  }]}]

        _apply_servers(client, state, teams)
        server = client.post.call_args[0][1]["Server"]
        assert server["authkey"] == "my-explicit-key-from-config-abcdef1234567890"

    def test_explicit_authkey_sent_on_update(self):
        """Config authkey should always be sent on update (even though we can't diff it)."""
        client = MagicMock()
        state = MagicMock()
        state.org_id = lambda uuid: 5
        state.users = {}
        state.tag_id = lambda name: None
        state.servers = {"https://remote.com": {
            "id": 1, "name": "Remote", "internal": False,
            "push": False, "pull": True, "self_signed": False,
            "caching_enabled": False, "remote_org_id": 5,
            "pull_rules": json.dumps({"tags": {"OR": [], "NOT": []},
                                       "orgs": {"OR": [], "NOT": []},
                                       "type_attributes": {"NOT": []},
                                       "type_objects": {"NOT": []},
                                       "url_params": ""}),
            "push_rules": json.dumps({"tags": {"OR": [], "NOT": []},
                                       "orgs": {"OR": [], "NOT": []}}),
            "authkey": "",  # MISP doesn't return it
        }}

        teams = [{"uuid": "ee8e368c-1587-4630-99f7-0b7a3ff2674f", "name": "T",
                  "sync_users": [], "users": [], "feide_users": [],
                  "service_users": [], "tags": [],
                  "servers": [{
                      "name": "Remote", "url": "https://remote.com",
                      "authkey": "new-key-from-config-0000000000000000000000",
                      "pull": True, "push": False,
                      "internal": False, "self_signed": False, "caching_enabled": False,
                      "pull_tags": [], "push_tags": [],
                      "pull_tags_exclude": [], "push_tags_exclude": [],
                  }]}]

        counts, _ = _apply_servers(client, state, teams)
        # Authkey-only change doesn't count as "updated" in summary
        assert counts["updated"] == 0
        # But the authkey IS sent to MISP
        server_update = client.post.call_args[0][1]["Server"]
        assert server_update["authkey"] == "new-key-from-config-0000000000000000000000"

    def test_env_var_in_authkey(self):
        """Authkey should support ${ENV_VAR} expansion."""
        os.environ["TEST_SERVER_KEY"] = "env-injected-key-for-remote-server-000"
        try:
            config = load_config(file_path="/dev/null")  # empty
            # Build a config manually with env var
            raw = {
                "teams": [{
                    "uuid": "4f1ed2b2-1821-49da-bf2c-b7ab639d9b19",
                    "name": "T",
                    "servers": [{"name": "S", "url": "https://x.com",
                                 "authkey": "${TEST_SERVER_KEY}"}],
                }]
            }
            from misp_container.sync import _expand_env_recursive, _normalize
            result = _normalize(_expand_env_recursive(raw))
            assert result["teams"][0]["servers"][0]["authkey"] == "env-injected-key-for-remote-server-000"
        finally:
            del os.environ["TEST_SERVER_KEY"]


# ---------------------------------------------------------------------------
# Apply taxonomies / warninglists
# ---------------------------------------------------------------------------

class TestApplyTaxonomies:
    @staticmethod
    def _mock_client(taxonomies):
        """Create a mock client that returns the given taxonomies on refresh."""
        client = MagicMock()
        client.get_taxonomies.return_value = taxonomies
        return client

    def test_enables_configured_taxonomy(self):
        tax = {"tlp": {"id": 1, "enabled": False, "namespace": "tlp"}}
        client = self._mock_client(tax)
        state = MagicMock()
        state.taxonomies = dict(tax)
        counts = _apply_taxonomies(client, state, ["tlp"])
        assert counts["enabled"] == 1

    def test_disables_unmanaged_taxonomy(self):
        tax = {"old": {"id": 2, "enabled": True, "namespace": "old"}}
        client = self._mock_client(tax)
        state = MagicMock()
        state.taxonomies = dict(tax)
        counts = _apply_taxonomies(client, state, [])
        assert counts["disabled"] == 1

    def test_warns_on_missing_taxonomy(self):
        client = self._mock_client({})
        state = MagicMock()
        state.taxonomies = {}
        counts = _apply_taxonomies(client, state, ["nonexistent"])
        assert counts["enabled"] == 0

    def test_skips_already_enabled(self):
        tax = {"tlp": {"id": 1, "enabled": True, "namespace": "tlp"}}
        client = self._mock_client(tax)
        state = MagicMock()
        state.taxonomies = dict(tax)
        counts = _apply_taxonomies(client, state, ["tlp"])
        assert counts["enabled"] == 0


class TestApplyWarninglists:
    def test_enables_configured_warninglist(self):
        client = MagicMock()
        state = MagicMock()
        state.warninglists = {"Top 1000": {"id": 5, "enabled": False}}
        counts = _apply_warninglists(client, state, ["Top 1000"])
        assert counts["enabled"] == 1

    def test_disables_unmanaged_warninglist(self):
        client = MagicMock()
        state = MagicMock()
        state.warninglists = {"Old list": {"id": 6, "enabled": True}}
        counts = _apply_warninglists(client, state, [])
        assert counts["disabled"] == 1


# ---------------------------------------------------------------------------
# Apply sharing groups
# ---------------------------------------------------------------------------

class TestApplySharingGroups:
    def test_creates_sharing_group_with_orgs(self):
        client = MagicMock()
        client.post.return_value = {"SharingGroup": {"id": 42}}
        state = MagicMock()
        state.sharing_groups = {}
        state.org_id = lambda uuid: {"ee8e368c-1587-4630-99f7-0b7a3ff2674f": 1, "cb610191-6a92-4502-b5c9-9ab0b0153881": 2}.get(uuid.lower() if len(uuid) > 10 else uuid)

        desired = [{
            "name": "Test SG", "uuid": "128f1153-ccf9-4dc4-87e1-5307d794764c",
            "description": "A sharing group", "releasability": "",
            "active": True, "roaming": False, "local": True,
            "organisations": ["ee8e368c-1587-4630-99f7-0b7a3ff2674f", {"uuid": "cb610191-6a92-4502-b5c9-9ab0b0153881", "extend": True}],
        }]

        counts = _apply_sharing_groups(client, state, desired)
        assert counts["created"] == 1

        # Should call: 1 create SG + 2 addOrg
        assert client.post.call_count == 3
        add_org_calls = [c for c in client.post.call_args_list
                         if "addOrg" in c[0][0]]
        assert len(add_org_calls) == 2


class TestSyncSgOrgs:
    def test_adds_missing_and_removes_extra(self):
        client = MagicMock()
        state = MagicMock()
        state.org_id = lambda uuid: {"ee8e368c-1587-4630-99f7-0b7a3ff2674f": 1, "cb610191-6a92-4502-b5c9-9ab0b0153881": 2, "f3b1eff3-f14e-4b3a-83f7-c31cd2720d9f": 3}.get(uuid.lower() if len(uuid) > 10 else uuid)

        # Current: orgs 1 and 3. Desired: orgs 1 and 2.
        current_orgs = [
            {"Organisation": {"id": 1}},
            {"Organisation": {"id": 3}},
        ]
        desired_orgs = ["ee8e368c-1587-4630-99f7-0b7a3ff2674f", "cb610191-6a92-4502-b5c9-9ab0b0153881"]

        _sync_sg_orgs(client, state, sg_id=10, desired_orgs=desired_orgs,
                       current_orgs=current_orgs)

        calls = client.post.call_args_list
        add_calls = [c for c in calls if "addOrg" in c[0][0]]
        remove_calls = [c for c in calls if "removeOrg" in c[0][0]]
        assert len(add_calls) == 1  # org 2 added
        assert len(remove_calls) == 1  # org 3 removed
        assert add_calls[0][0][1]["org_id"] == 2
        assert remove_calls[0][0][1]["org_id"] == 3


# ---------------------------------------------------------------------------
# Disable unmanaged
# ---------------------------------------------------------------------------

class TestDisableUnmanaged:
    @patch("misp_container.sync.env", return_value="admin@admin.test")
    def test_disables_unmanaged_users(self, mock_env):
        client = MagicMock()
        state = MagicMock()
        state.users = {
            "admin@admin.test": {"id": 1, "disabled": False},  # ID 1: never disabled
            "managed@x.com": {"id": 2, "disabled": False},
            "unmanaged@x.com": {"id": 3, "disabled": False},
        }
        count = _disable_unmanaged_users(client, state, {"managed@x.com"})
        assert count == 1

        # Verify the right user was disabled (id=3, not id=1)
        path, data = client.post.call_args[0]
        assert "/users/edit/3" in path
        assert data["User"]["disabled"] is True

    @patch("misp_container.sync.env", return_value="admin@admin.test")
    def test_skips_already_disabled_users(self, mock_env):
        client = MagicMock()
        state = MagicMock()
        state.users = {
            "unmanaged@x.com": {"id": 2, "disabled": True},
        }
        count = _disable_unmanaged_users(client, state, set())
        assert count == 0
        client.post.assert_not_called()

    def test_disables_unmanaged_servers(self):
        client = MagicMock()
        state = MagicMock()
        state.servers = {
            "https://managed.com": {"id": 1, "push": True, "pull": True},
            "https://unmanaged.com": {"id": 2, "push": True, "pull": False},
        }
        count = _disable_unmanaged_servers(client, state, {"https://managed.com"})
        assert count == 1

        path, data = client.post.call_args[0]
        assert "/servers/edit/2" in path
        assert data["Server"]["push"] is False
        assert data["Server"]["pull"] is False

    def test_skips_already_disabled_servers(self):
        client = MagicMock()
        state = MagicMock()
        state.servers = {
            "https://idle.com": {"id": 3, "push": False, "pull": False},
        }
        count = _disable_unmanaged_servers(client, state, set())
        assert count == 0
        client.post.assert_not_called()


# ---------------------------------------------------------------------------
# Full orchestrator
# ---------------------------------------------------------------------------

class TestApplyOrchestrator:
    """Test the full apply() flow with a realistic config and mock client."""

    def _make_state(self):
        state = MagicMock(spec=MISPState)
        state.orgs = {}
        state.users = {}
        state.roles = {"User": {"id": "3", "name": "User"},
                       "Sync user": {"id": "4", "name": "Sync user"}}
        state.servers = {}
        state.tags = {}
        state.taxonomies = {"tlp": {"id": 1, "enabled": False, "namespace": "tlp"}}
        state.warninglists = {}
        state.sharing_groups = {}
        state.role_id = lambda name: {"User": 3, "Sync user": 4}.get(name)
        state.org_id = lambda uuid: None  # initially no orgs
        state.tag_id = lambda name: None
        state.refresh_tags = MagicMock()
        state.refresh_orgs = MagicMock()
        state.refresh_users = MagicMock()
        return state

    @patch("misp_container.sync.MISPState")
    @patch("misp_container.sync.env")
    def test_full_apply_creates_org_with_user(self, mock_env, MockState):
        mock_env.side_effect = lambda k, d="": {
            "BASE_URL": "http://localhost:8080",
            "ADMIN_KEY": "test-key",
            "ADMIN_EMAIL": "admin@admin.test",
        }.get(k, d)

        state = self._make_state()
        MockState.return_value = state

        client = MagicMock()
        # Taxonomy update/refresh returns the same taxonomies
        client.get_taxonomies.return_value = state.taxonomies

        config = _normalize({
            "teams": [{
                "uuid": "f3b1eff3-f14e-4b3a-83f7-c31cd2720d9f",
                "name": "Test Team",
                "users": [{"email": "user@team.com", "role": "User"}],
            }],
            "taxonomies": ["tlp"],
        })

        # After org creation, org_id should be resolvable
        def org_id_after_create(uuid):
            if uuid == "f3b1eff3-f14e-4b3a-83f7-c31cd2720d9f" and state.refresh_orgs.called:
                return 5
            return None
        state.org_id = org_id_after_create

        summary = apply(client=client, config=config)

        assert "organisations" in summary
        assert summary["organisations"]["created"] == 1
        assert summary["taxonomies"]["enabled"] == 1

        # Verify org was created with correct data
        org_call = client.post.call_args_list[0]
        assert "organisations/add" in org_call[0][0]
        assert org_call[0][1]["Organisation"]["name"] == "Test Team"
