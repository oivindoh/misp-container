"""Unit tests for the MISP 3.0 upgrade step (misp_container.upgrade) and the configure step's layout check."""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import configure, db, upgrade


@pytest.fixture
def steps(monkeypatch):
    """Record the side effects of a run instead of performing them."""
    steps = []
    monkeypatch.setenv("UPGRADE_BACKUP_CONFIRMED", "true")
    monkeypatch.setattr(db, "wait_for_db", lambda: None)
    monkeypatch.setattr(db, "is_postgres", lambda: False)
    monkeypatch.setattr(db, "acquire_config_lock", lambda: steps.append("lock"))
    monkeypatch.setattr(db, "release_config_lock", lambda: steps.append("unlock"))
    monkeypatch.setattr(db, "execute", lambda sql, params=None: steps.append(sql))
    monkeypatch.setattr(upgrade, "setup_tmp", lambda: None)
    monkeypatch.setattr(upgrade, "prepare_config", lambda: steps.append("app/Config"))
    monkeypatch.setattr(upgrade.cake, "set_setting", lambda name, value: steps.append(f"{name}={value}"))
    monkeypatch.setattr(upgrade, "record", lambda status, started, error="": steps.append(f"record {status}"))
    return steps


def converts_in(monkeypatch, steps, returncode=0, converted=True):
    """runUpgrade returns returncode; the database then needs no conversion when converted."""
    state = {"text_values": True}

    def run(cmd):
        steps.append(" ".join(cmd[1:]))
        state["text_values"] = not converted
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(upgrade, "needs_conversion", lambda: state["text_values"])
    monkeypatch.setattr(upgrade.subprocess, "run", run)


class TestUpgrade:
    def test_an_unconfirmed_backup_exits_2_before_touching_the_database(self, monkeypatch, steps):
        monkeypatch.setenv("UPGRADE_BACKUP_CONFIRMED", "false")
        monkeypatch.setattr(db, "wait_for_db", lambda: pytest.fail("reached the database"))
        with pytest.raises(SystemExit) as exc:
            upgrade.run()
        assert exc.value.code == upgrade.EXIT_UNCONFIRMED

    def test_a_converted_database_is_left_alone(self, monkeypatch, steps):
        monkeypatch.setattr(upgrade, "needs_conversion", lambda: False)
        monkeypatch.setattr(upgrade.subprocess, "run", lambda cmd: pytest.fail("ran runUpgrade"))
        upgrade.run()
        assert steps == []

    def test_a_25_database_is_converted_under_the_lock(self, monkeypatch, steps):
        converts_in(monkeypatch, steps)
        upgrade.run()
        assert steps == ["app/Config", "lock", "MISP.osuser=misp", "Admin runUpgrade", "unlock", "record success"]

    def test_postgres_gets_pg_trgm_before_the_upgrade(self, monkeypatch, steps):
        monkeypatch.setattr(db, "is_postgres", lambda: True)
        converts_in(monkeypatch, steps)
        upgrade.run()
        assert steps.index("CREATE EXTENSION IF NOT EXISTS pg_trgm") < steps.index("Admin runUpgrade")

    def test_a_failed_run_exits_1_and_releases_the_lock(self, monkeypatch, steps):
        converts_in(monkeypatch, steps, returncode=1, converted=False)
        with pytest.raises(SystemExit) as exc:
            upgrade.run()
        assert exc.value.code == upgrade.EXIT_FAILED
        assert "unlock" in steps and "record error" in steps

    def test_a_run_that_leaves_text_values_counts_as_failed(self, monkeypatch, steps):
        converts_in(monkeypatch, steps, returncode=0, converted=False)
        with pytest.raises(SystemExit) as exc:
            upgrade.run()
        assert exc.value.code == upgrade.EXIT_FAILED

    def test_an_empty_database_needs_no_conversion(self, monkeypatch):
        monkeypatch.setattr(db, "query_ok", lambda sql: False)
        monkeypatch.setattr(db, "has_column", lambda t, c: pytest.fail("looked for a column"))
        assert upgrade.needs_conversion() is False

    def test_text_values_need_conversion(self, monkeypatch):
        monkeypatch.setattr(db, "query_ok", lambda sql: True)
        monkeypatch.setattr(db, "has_column", lambda t, c: (t, c) == ("attributes", "value1"))
        assert upgrade.needs_conversion() is True


class TestLayoutCheck:
    def test_a_30_image_refuses_a_25_database(self, monkeypatch):
        monkeypatch.setattr(configure, "misp_major", lambda: 3)
        monkeypatch.setattr(configure, "needs_conversion", lambda: True)
        with pytest.raises(SystemExit):
            configure.check_layout()

    def test_a_30_image_takes_a_30_database(self, monkeypatch):
        monkeypatch.setattr(configure, "misp_major", lambda: 3)
        monkeypatch.setattr(configure, "needs_conversion", lambda: False)
        configure.check_layout()

    def test_a_25_image_does_not_look(self, monkeypatch):
        monkeypatch.setattr(configure, "misp_major", lambda: 2)
        monkeypatch.setattr(configure, "needs_conversion", lambda: pytest.fail("looked at the layout"))
        configure.check_layout()


class TestHasColumn:
    @pytest.mark.parametrize("postgres,schema", [(False, "DATABASE()"), (True, "current_schema()")])
    def test_the_lookup_takes_the_engine_schema_and_parameters(self, monkeypatch, postgres, schema):
        seen = {}

        def dict_query(sql, params=None):
            seen.update(sql=sql, params=params)
            return [{"present": 1}]

        monkeypatch.setattr(db, "is_postgres", lambda: postgres)
        monkeypatch.setattr(db, "dict_query", dict_query)
        assert db.has_column("attributes", "value1") is True
        assert f"table_schema = {schema}" in seen["sql"] and seen["params"] == ("attributes", "value1")
