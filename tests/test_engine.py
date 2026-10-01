"""Unit tests for the engine-neutral database helpers and housekeeping."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import db, housekeeping


class TestEngineSelection:
    def test_default_is_mysql(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DB_ENGINE", None)
            assert db.engine() == db.MYSQL and not db.is_postgres()

    @pytest.mark.parametrize("value", ["postgres", "PostgreSQL", "pgsql"])
    def test_postgres_spellings(self, value):
        with patch.dict(os.environ, {"DB_ENGINE": value}):
            assert db.is_postgres()

    def test_default_port_follows_engine(self):
        with patch.dict(os.environ, {"DB_ENGINE": "postgres", "DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": "p", "DB_NAME": "d"}):
            os.environ.pop("DB_PORT", None)
            assert db.settings()["port"] == 5432
        with patch.dict(os.environ, {"DB_ENGINE": "mysql", "DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": "p", "DB_NAME": "d"}):
            os.environ.pop("DB_PORT", None)
            assert db.settings()["port"] == 3306


class TestSqlFragments:
    def test_mysql(self):
        with patch.dict(os.environ, {"DB_ENGINE": "mysql"}):
            assert db.bool_lit(True) == "1" and db.bool_lit(False) == "0"
            assert db.now_epoch() == "UNIX_TIMESTAMP()"
            assert db.epoch("x") == "UNIX_TIMESTAMP(x)"
            assert db.ago(2, "DAY") == "NOW() - INTERVAL 2 DAY"

    def test_postgres(self):
        with patch.dict(os.environ, {"DB_ENGINE": "postgres"}):
            assert db.bool_lit(True) == "TRUE" and db.bool_lit(False) == "FALSE"
            assert db.now_epoch() == "EXTRACT(EPOCH FROM NOW())::bigint"
            assert db.epoch("x") == "EXTRACT(EPOCH FROM x)::bigint"
            assert db.ago(24, "HOUR") == "NOW() - INTERVAL '24 hours'"


class TestAliases:
    def test_mysql_names_fill_db_names(self):
        from misp_container.env import apply_defaults
        env = {"MYSQL_HOST": "legacy", "MYSQL_DATABASE": "misp", "MYSQL_USER": "u", "MYSQL_PASSWORD": "p"}
        with patch.dict(os.environ, env, clear=False):
            for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_ENGINE"):
                os.environ.pop(key, None)
            apply_defaults()
            assert os.environ["DB_HOST"] == "legacy"
            assert os.environ["DB_NAME"] == "misp"
            assert os.environ["DB_ENGINE"] == "mysql"


class TestHousekeeping:
    def test_batches_until_a_partial_batch(self, monkeypatch):
        calls = iter([25000, 3000])
        executed = []
        monkeypatch.setattr(db, "execute", lambda sql, params=None: (executed.append(sql), next(calls))[1])
        with patch.dict(os.environ, {"DB_ENGINE": "mysql", "HOUSEKEEPING_JOBS_DAYS": "5"}):
            total = housekeeping.run("jobs")
        assert total == 28000
        assert len(executed) == 2
        assert "date_created < NOW() - INTERVAL 5 DAY LIMIT 25000" in executed[0]

    def test_an_empty_retention_variable_takes_the_default(self, monkeypatch):
        with patch.dict(os.environ, {"HOUSEKEEPING_LOGS_DAYS": ""}):
            assert housekeeping.retention_days("logs") == 30

    def test_postgres_delete_uses_subselect(self, monkeypatch):
        executed = []
        monkeypatch.setattr(db, "execute", lambda sql, params=None: (executed.append(sql), 0)[1])
        with patch.dict(os.environ, {"DB_ENGINE": "postgres"}):
            housekeeping.run("audit_logs")
        assert "DELETE FROM audit_logs WHERE id IN (SELECT id FROM audit_logs WHERE created < NOW() - INTERVAL '90 days'" in executed[0]

    def test_unknown_table_exits(self):
        with pytest.raises(SystemExit) as exc:
            housekeeping.main(["users"])
        assert exc.value.code == 2


class TestCursorHandling:
    """pg8000 cursors have no __enter__; every query path must still close them."""

    class _Cursor:
        def __init__(self):
            self.closed = False
            self.description = [("n",)]
            self.rowcount = 1

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return [(1,)]

        def fetchone(self):
            return (1,)

        def close(self):
            self.closed = True

    class _Conn:
        def __init__(self, cursor):
            self._cursor = cursor
            self.closed = False

        def cursor(self):
            return self._cursor

        def close(self):
            self.closed = True

    def test_query_paths_work_without_context_manager(self, monkeypatch):
        cursors = []

        def connect(autocommit=True):
            cur = self._Cursor()
            cursors.append(cur)
            return self._Conn(cur)

        monkeypatch.setattr(db, "_connect", connect)
        assert db.query("SELECT 1") == "1"
        assert db.dict_query("SELECT 1 AS n") == [{"n": 1}]
        assert db.execute("UPDATE t SET a = 1") == 1
        assert db.query_ok("SELECT 1") is True
        assert all(c.closed for c in cursors)
