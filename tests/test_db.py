"""Unit tests for the advisory lock (no MySQL needed)."""

import os
import sys
import types
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import db


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append(sql)

    def fetchone(self):
        return (self.conn.lock_result,)


class FakeConnection:
    def __init__(self, lock_result=1):
        self.lock_result = lock_result
        self.statements = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pymysql():
    connections = []

    def connect(**kwargs):
        conn = FakeConnection(lock_result=connect.lock_result)
        connections.append(conn)
        return conn

    connect.lock_result = 1
    module = types.ModuleType("pymysql")
    module.connect = connect
    module.cursors = types.ModuleType("pymysql.cursors")
    env = {"MYSQL_HOST": "h", "MYSQL_PORT": "3306", "MYSQL_USER": "u",
           "MYSQL_PASSWORD": "p", "MYSQL_DATABASE": "d"}
    with patch.dict(sys.modules, {"pymysql": module, "pymysql.cursors": module.cursors}), \
            patch.dict(os.environ, env):
        db._lock_conn = None
        yield connect, connections
        db._lock_conn = None


class TestConfigLock:
    def test_lock_connection_stays_open_until_release(self, fake_pymysql):
        """GET_LOCK and RELEASE_LOCK run on one connection that only closes on release."""
        _, connections = fake_pymysql
        db.acquire_config_lock()
        assert len(connections) == 1
        conn = connections[0]
        assert not conn.closed
        assert any("GET_LOCK" in s for s in conn.statements)

        db.release_config_lock()
        assert conn.closed
        assert any("RELEASE_LOCK" in s for s in conn.statements)
        assert len(connections) == 1

    def test_lock_failure_exits(self, fake_pymysql):
        """A lock that cannot be taken within the timeout stops the entrypoint."""
        connect, connections = fake_pymysql
        connect.lock_result = 0
        with pytest.raises(SystemExit):
            db.acquire_config_lock()
        assert connections[0].closed

    def test_release_without_lock_is_noop(self, fake_pymysql):
        _, connections = fake_pymysql
        db.release_config_lock()
        assert connections == []


class TestSplitSqlStatements:
    def test_semicolons_in_comments_and_strings(self):
        sql = (
            "-- header; with a semicolon\n"
            "/*!40101 SET NAMES utf8mb4 */;\n"
            "CREATE TABLE t (\n  a INT -- trailing; comment\n);\n"
            "# hash; comment\n"
            "/* block; comment */\n"
            "INSERT INTO t VALUES ('a;b', \"c;d\", 'it''s', 'back\\'slash;');\n"
            "SELECT `x;y` FROM t"
        )
        got = db.split_sql_statements(sql)
        assert got == [
            "/*!40101 SET NAMES utf8mb4 */",
            "CREATE TABLE t (\n  a INT \n)",
            "INSERT INTO t VALUES ('a;b', \"c;d\", 'it''s', 'back\\'slash;')",
            "SELECT `x;y` FROM t",
        ]

    def test_empty_and_comment_only_input(self):
        assert db.split_sql_statements("-- only; a comment\n/* and; this */\n") == []


class TestWaitForLive:
    """Pods wait for MISP.live and for the configure run of their own image version."""

    def _rows(self, live, version):
        rows = []
        if live is not None:
            rows.append(("MISP.live", live))
        if version is not None:
            rows.append(("misp_docker.defaults_version", version))
        return rows

    def _run(self, tmp_path, monkeypatch, sequences, image_version="v2.5.47"):
        marker = tmp_path / "version"
        marker.write_text(image_version)
        monkeypatch.setattr("misp_container.DIST_VERSION_FILE", str(marker))
        calls = iter(sequences)
        outputs = []
        def fake_query(sql, check=False):
            rows = next(calls)
            return "\n".join(f"{k}\t{v}" for k, v in rows)
        monkeypatch.setattr(db, "query", fake_query)
        monkeypatch.setattr(db.time, "sleep", lambda s: None)
        return db.wait_for_live(retries=len(sequences), wait_seconds=0)

    def test_waits_for_own_version(self, tmp_path, monkeypatch):
        self._run(tmp_path, monkeypatch, [
            self._rows('"true"', '"v2.5.37"'),   # old configure run: keep waiting
            self._rows('"true"', '"v2.5.47"'),   # this image's run: go
        ])

    def test_live_alone_is_not_enough(self, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            self._run(tmp_path, monkeypatch, [self._rows('"true"', '"v2.5.37"')] * 2)

    def test_unknown_image_version_waits_for_live_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr("misp_container.DIST_VERSION_FILE", str(tmp_path / "absent"))
        outputs = iter([self._rows('"true"', '"v2.5.37"')])
        monkeypatch.setattr(db, "query", lambda sql, check=False: "\n".join(f"{k}\t{v}" for k, v in next(outputs)))
        monkeypatch.setattr(db.time, "sleep", lambda s: None)
        db.wait_for_live(retries=1, wait_seconds=0)
