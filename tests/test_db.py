"""Unit tests for the database layer and housekeeping (no PostgreSQL needed)."""

import json
import os
import ssl
import sys
import types
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import db, housekeeping


class FakeCursor:
    """Shaped like a pg8000 cursor: close(), no context manager."""

    def __init__(self, conn):
        self.conn = conn

    def close(self):
        pass

    def execute(self, sql, params=None):
        self.conn.statements.append(sql)

    def fetchone(self):
        return (self.conn.lock_result,)


class FakeConnection:
    def __init__(self, lock_result=True):
        self.lock_result = lock_result
        self.statements = []
        self.closed = False
        self.autocommit = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pg8000():
    connections = []

    def connect(**kwargs):
        conn = FakeConnection(lock_result=connect.lock_result)
        connections.append(conn)
        return conn

    connect.lock_result = True
    dbapi = types.ModuleType("pg8000.dbapi")
    dbapi.connect = connect
    package = types.ModuleType("pg8000")
    package.dbapi = dbapi
    env = {"DB_HOST": "h", "DB_PORT": "5432", "DB_USER": "u", "DB_PASSWORD": "p", "DB_NAME": "d"}
    with patch.dict(sys.modules, {"pg8000": package, "pg8000.dbapi": dbapi}), patch.dict(os.environ, env):
        db._lock_conn = None
        yield connect, connections
        db._lock_conn = None


class TestConfigLock:
    def test_lock_connection_stays_open_until_release(self, fake_pg8000):
        """The advisory lock and its release run on one connection that only closes on release."""
        _, connections = fake_pg8000
        db.acquire_config_lock()
        assert len(connections) == 1
        conn = connections[0]
        assert not conn.closed
        assert any("pg_try_advisory_lock" in s for s in conn.statements)

        db.release_config_lock()
        assert conn.closed
        assert any("pg_advisory_unlock" in s for s in conn.statements)
        assert len(connections) == 1

    def test_lock_failure_exits(self, fake_pg8000):
        """A lock that cannot be taken within the timeout stops the entrypoint."""
        connect, connections = fake_pg8000
        connect.lock_result = False
        with patch.object(db.time, "sleep", lambda s: None), pytest.raises(SystemExit):
            db.acquire_config_lock(timeout=0)
        assert connections[0].closed

    def test_release_without_lock_is_noop(self, fake_pg8000):
        _, connections = fake_pg8000
        db.release_config_lock()
        assert connections == []


def test_an_empty_port_takes_postgres_default():
    with patch.dict(os.environ, {"DB_HOST": "h", "DB_PORT": ""}):
        assert db.settings()["port"] == 5432


class TestTls:
    """DB_SSLMODE and DB_SSLROOTCERT, in libpq's terms."""

    @staticmethod
    def settings(**env):
        with patch.dict(os.environ, {"DB_HOST": "h", "DB_SSLMODE": "", "DB_SSLROOTCERT": "", **env}):
            return db.settings()

    def test_prefer_is_the_default(self):
        assert self.settings()["sslmode"] == "prefer"

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="DB_SSLMODE=true: use one of disable, prefer"):
            self.settings(DB_SSLMODE="true")

    def test_verify_ca_needs_a_root_certificate(self):
        with pytest.raises(ValueError, match="needs DB_SSLROOTCERT"):
            self.settings(DB_SSLMODE="verify-ca")
        assert self.settings(DB_SSLMODE="verify-ca", DB_SSLROOTCERT="/ca.crt")["sslrootcert"] == "/ca.crt"

    @pytest.mark.parametrize("mode, value", [("disable", False), ("prefer", None), ("require", True)])
    def test_the_modes_without_a_check_take_pg8000s_own_values(self, mode, value):
        assert db.ssl_context(mode) is value

    def test_verify_full_checks_the_name_against_the_system_cas(self):
        context = db.ssl_context("verify-full")
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

    def test_verify_ca_checks_the_chain_against_the_root_certificate(self):
        with patch("ssl.create_default_context", return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)) as create:
            context = db.ssl_context("verify-ca", "/ca.crt")
        create.assert_called_once_with(cafile="/ca.crt")
        assert context.verify_mode == ssl.CERT_REQUIRED and not context.check_hostname

    def test_the_connection_takes_the_mode(self, fake_pg8000):
        dbapi = sys.modules["pg8000.dbapi"]
        fake_connect, contexts = dbapi.connect, []

        def connect(**kwargs):
            contexts.append(kwargs.pop("ssl_context"))
            return fake_connect(**kwargs)

        dbapi.connect = connect
        with patch.dict(os.environ, {"DB_SSLMODE": "disable"}):
            db._connect()
        assert contexts == [False]


class TestHousekeeping:
    def test_batches_until_a_partial_batch(self, monkeypatch):
        calls = iter([25000, 3000])
        executed = []
        monkeypatch.setattr(db, "execute", lambda sql, params=None: (executed.append(sql), next(calls))[1])
        with patch.dict(os.environ, {"HOUSEKEEPING_JOBS_DAYS": "5"}):
            total = housekeeping.run("jobs")
        assert total == 28000
        assert len(executed) == 2

    def test_an_empty_retention_variable_takes_the_default(self, monkeypatch):
        with patch.dict(os.environ, {"HOUSEKEEPING_LOGS_DAYS": ""}):
            assert housekeeping.retention_days("logs") == 30

    def test_delete_uses_a_subselect(self, monkeypatch):
        executed = []
        monkeypatch.setattr(db, "execute", lambda sql, params=None: (executed.append(sql), 0)[1])
        housekeeping.run("audit_logs")
        assert ("DELETE FROM audit_logs WHERE id IN (SELECT id FROM audit_logs WHERE created < "
                "NOW() - INTERVAL '90 days' ORDER BY id LIMIT 25000)") in executed[0]

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
        assert db.dict_query("SELECT 1 AS n") == [{"n": 1}]
        assert db.execute("UPDATE t SET a = 1") == 1
        assert db.query_ok("SELECT 1") is True
        assert all(c.closed for c in cursors)


class TestSystemSettings:
    @patch("misp_container.db.execute")
    def test_a_value_is_written_json_encoded_as_bytes(self, execute):
        db.set_system_setting("misp_docker.defaults_version", "v2.5.50")
        sql, params = execute.call_args[0]
        assert "ON CONFLICT (setting) DO UPDATE" in sql
        assert params == ("misp_docker.defaults_version", b'"v2.5.50"')

    @patch("misp_container.db.dict_query", return_value=[{"value": '{"success": 2}'}])
    def test_a_value_is_read_json_decoded(self, query):
        assert db.get_system_setting("misp_docker.runs.configure") == {"success": 2}

    @patch("misp_container.db.dict_query", return_value=[])
    def test_a_missing_row_reads_none(self, query):
        assert db.get_system_setting("misp_docker.runs.configure") is None


class TestRunRecords:
    @patch("misp_container.db.set_system_setting")
    @patch("misp_container.db.get_system_setting", return_value={"success": 2, "last_success": 1})
    def test_a_run_adds_one_to_its_status(self, get, put):
        with patch.object(db.time, "time", return_value=1000.5):
            db.record_run("org-sync", False)
        put.assert_called_once_with("misp_docker.runs.org-sync",
                                    {"success": 2, "last_success": 1, "error": 1, "last_error": 1000})

    @patch("misp_container.db.set_system_setting")
    @patch("misp_container.db.get_system_setting", return_value="not a record")
    def test_an_unreadable_row_starts_over(self, get, put):
        db.record_run("configure", True)
        assert put.call_args[0][1]["success"] == 1

    def test_rows_parse_by_operation(self):
        rows = [{"setting": "misp_docker.runs.migrate", "value": b'{"success": 1, "last_success": 5}'},
                {"setting": "misp_docker.runs.broken", "value": "not json"}]
        assert db.parse_run_records(rows) == {"migrate": {"success": 1, "last_success": 5}}


class TestWaitForConfigure:
    """Pods wait for the configure run of their own image version and configuration checksum."""

    def _run(self, tmp_path, monkeypatch, sequences, image_version="3.0_wip+abc", checksum="c1"):
        marker = tmp_path / "version"
        marker.write_text(image_version)
        monkeypatch.setattr("misp_container.DIST_VERSION_FILE", str(marker))
        monkeypatch.setenv("MISP_CONFIG_CHECKSUM", checksum)
        calls = iter(sequences)

        def fake_dict_query(sql, params=None):
            return [{"setting": k, "value": json.dumps(v)} for k, v in next(calls).items()]

        monkeypatch.setattr(db, "dict_query", fake_dict_query)
        monkeypatch.setattr(db.time, "sleep", lambda s: None)
        return db.wait_for_configure(retries=len(sequences), wait_seconds=0)

    def test_waits_for_its_own_version_and_checksum(self, tmp_path, monkeypatch):
        self._run(tmp_path, monkeypatch, [
            {db.VERSION_ROW: "3.0_wip+old", db.CHECKSUM_ROW: "c1"},   # an older image's run
            {db.VERSION_ROW: "3.0_wip+abc", db.CHECKSUM_ROW: "c0"},   # an older configuration's run
            {db.VERSION_ROW: "3.0_wip+abc", db.CHECKSUM_ROW: "c1"},   # its own run: go
        ])

    def test_gives_up_without_its_own_run(self, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            self._run(tmp_path, monkeypatch, [{db.VERSION_ROW: "3.0_wip+abc", db.CHECKSUM_ROW: "c0"}] * 2)

    def test_misp_live_plays_no_part(self, tmp_path, monkeypatch):
        # MISP.live is MISP's maintenance switch; a pod restarted during maintenance still starts
        self._run(tmp_path, monkeypatch, [{db.VERSION_ROW: "3.0_wip+abc", db.CHECKSUM_ROW: "c1", "MISP.live": False}])

    def test_without_a_checksum_the_version_decides(self, tmp_path, monkeypatch):
        self._run(tmp_path, monkeypatch, [{db.VERSION_ROW: "3.0_wip+abc"}], checksum="")


class TestHasColumn:
    def test_the_lookup_takes_the_current_schema_and_parameters(self, monkeypatch):
        seen = {}

        def dict_query(sql, params=None):
            seen.update(sql=sql, params=params)
            return [{"present": 1}]

        monkeypatch.setattr(db, "dict_query", dict_query)
        assert db.has_column("attributes", "value1") is True
        assert "table_schema = current_schema()" in seen["sql"] and seen["params"] == ("attributes", "value1")
