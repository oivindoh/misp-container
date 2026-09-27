"""Unit tests for the migration Job (misp_container.migrate)."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import db, migrate


class TestConversion:
    def test_boolean_from_tinyint_and_strings(self):
        fn = migrate.coercer("boolean")
        assert fn(1) is True and fn(0) is False and fn(None) is None
        assert fn("1") is True and fn("0") is False and fn(b"1") is True

    def test_zero_date_becomes_null(self):
        fn = migrate.coercer("timestamp without time zone")
        assert fn("0000-00-00 00:00:00") is None
        assert fn("2026-01-01 00:00:00") == "2026-01-01 00:00:00"

    def test_bytea_from_bytearray(self):
        assert migrate.coercer("bytea")(bytearray(b"x")) == b"x"

    def test_other_types_pass_through(self):
        assert migrate.coercer("integer") is None
        assert migrate.coerce_row((1, "a"), None) == (1, "a")
        assert migrate.coerce_row((1, "a"), [migrate.to_bool, None]) == (True, "a")


class TestSql:
    def test_insert_quotes_per_engine(self):
        assert migrate.insert_sql("t", ["a", "b"], db.MYSQL) == "INSERT INTO `t` (`a`, `b`) VALUES (%s, %s)"
        assert migrate.insert_sql("t", ["a"], db.POSTGRES) == 'INSERT INTO "t" ("a") VALUES (%s)'

    def test_mysql8_collations_rewritten_for_mariadb(self):
        ddl = "CREATE TABLE `t` (`a` varchar(1) COLLATE utf8mb4_0900_ai_ci) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
        out = migrate.rewrite_ddl(ddl)
        assert "0900" not in out and "utf8mb4_unicode_ci" in out


class TestIdentity:
    def test_differing_salt_is_a_problem(self):
        problems = migrate.check_identity({"Security.salt": "a" * 32, "MISP.uuid": "u"},
                                          {"SECURITY_SALT": "b" * 32, "MISP_UUID": "u"})
        assert problems == ["Security.salt on the source differs from SECURITY_SALT in this deployment"]

    def test_missing_on_source_only_warns(self):
        assert migrate.check_identity({}, {"SECURITY_SALT": "x"}) == []

    def test_equal_values_pass(self):
        assert migrate.check_identity({"MISP.uuid": "u"}, {"MISP_UUID": "u"}) == []


class TestFiles:
    def test_plan_takes_event_dirs_only(self, tmp_path, monkeypatch):
        src = tmp_path / "files"
        for d in ("12", "7", "img/orgs", "terms", "taxonomies", "scripts"):
            (src / d).mkdir(parents=True)
        monkeypatch.setenv("MISP_ATTACHMENTS_DIR", str(tmp_path / "att"))
        plan = migrate.plan_files(src)
        targets = {s.relative_to(src).as_posix(): d for s, d in plan}
        assert targets == {"12": tmp_path / "att" / "12", "7": tmp_path / "att" / "7"}

    def test_copy_files_copies_trees(self, tmp_path, monkeypatch):
        src = tmp_path / "files"
        (src / "3" / "9").mkdir(parents=True)
        (src / "3" / "9" / "blob").write_bytes(b"data")
        monkeypatch.setenv("MISP_ATTACHMENTS_DIR", str(tmp_path / "att"))
        assert migrate.copy_files(src) == 1
        assert (tmp_path / "att" / "3" / "9" / "blob").read_bytes() == b"data"

    def test_missing_source_dir_is_empty_plan(self, tmp_path):
        assert migrate.plan_files(tmp_path / "nope") == []


class _ReadCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def execute(self, sql):
        self.sql = sql

    def fetchmany(self, n):
        out, self.rows = self.rows[:n], self.rows[n:]
        return out

    def close(self):
        pass


class _WriteCursor:
    def __init__(self):
        self.calls = []

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cls=None):
        return self._cursor


class TestCopyTable:
    def test_batches_and_coerces(self, monkeypatch):
        monkeypatch.setattr(migrate, "BATCH", 2)
        read = _ReadCursor([(1, 1, "x"), (2, 0, "y"), (3, None, "z")])
        write = _WriteCursor()
        n = migrate.copy_table(_Conn(read), _Conn(write), "t", ["id", "flag", "name"], db.POSTGRES,
                               {"id": "integer", "flag": "boolean", "name": "text"})
        assert n == 3
        assert read.sql == "SELECT `id`, `flag`, `name` FROM `t`"
        assert [len(rows) for _, rows in write.calls] == [2, 1]
        assert write.calls[0][1] == [(1, True, "x"), (2, False, "y")]
        assert write.calls[1][1] == [(3, None, "z")]

    def test_same_engine_rows_untouched(self):
        read = _ReadCursor([(1, 1)])
        write = _WriteCursor()
        migrate.copy_table(_Conn(read), _Conn(write), "t", ["id", "flag"], db.MYSQL)
        assert write.calls[0][1] == [(1, 1)]


class TestCrossEngineGate:
    def test_schema_mismatch_exits_5(self, monkeypatch):
        monkeypatch.setattr(db, "init_schema", lambda: None)
        monkeypatch.setattr(migrate, "source_schema_state", lambda c: ("120", set()))
        monkeypatch.setattr(migrate, "target_schema_state", lambda c, e: ("159", {"m1"}))
        with pytest.raises(SystemExit) as exc:
            migrate.migrate_cross_engine(object(), object())
        assert exc.value.code == migrate.EXIT_SCHEMA


class TestConfig:
    def test_missing_source_host_exits_2(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MIGRATE_SOURCE_HOST", None)
            with pytest.raises(SystemExit) as exc:
                migrate.source_settings()
        assert exc.value.code == migrate.EXIT_CONFIG

    def test_source_defaults(self):
        with patch.dict(os.environ, {"MIGRATE_SOURCE_HOST": "old", "MIGRATE_SOURCE_USER": "u",
                                     "MIGRATE_SOURCE_PASSWORD": "p"}):
            os.environ.pop("MIGRATE_SOURCE_PORT", None); os.environ.pop("MIGRATE_SOURCE_NAME", None)
            s = migrate.source_settings()
        assert s["port"] == 3306 and s["database"] == "misp" and s["tls"] is False
