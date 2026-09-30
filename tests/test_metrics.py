"""Unit tests for the Prometheus metrics exporter."""

import io
import os
import sys
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.metrics import (
    _escape_label,
    _metric,
    _collect_db_metrics,
    _collect_network_metrics,
    _collect_queue_metrics,
    _Redis,
    RedisError,
    _check_server_auth,
    _check_tls_cert,
    collect_all,
    init_sync_log_table,
    log_sync_result,
    _net_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_caches():
    """Force network cache expiry so tests always hit the collection path."""
    _net_cache.update(output="", ts=-(10**9))


def _parse_metrics(text):
    """Parse Prometheus text into {metric_name: [(labels_dict, value), ...]}."""
    result = {}
    current = None
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# HELP"):
            current = line.split()[2]
            result.setdefault(current, [])
        elif line.startswith("# TYPE"):
            continue
        elif current and not line.startswith("#"):
            # "name{k1="v1",k2="v2"} 42" or "name 42"
            if "{" in line:
                name_labels, value = line.rsplit("} ", 1)
                name, labels_str = name_labels.split("{", 1)
                labels = {}
                for pair in _split_label_pairs(labels_str):
                    k, v = pair.split("=", 1)
                    labels[k] = v.strip('"')
            else:
                parts = line.split()
                name = parts[0]
                value = parts[1]
                labels = {}
            result.setdefault(name, []).append((labels, value))
    return result


def _split_label_pairs(s):
    """Split 'k1="v1",k2="v2"' respecting escaped quotes."""
    pairs = []
    current = []
    in_quotes = False
    prev = None
    for ch in s:
        if ch == '"' and prev != '\\':
            in_quotes = not in_quotes
        if ch == ',' and not in_quotes:
            pairs.append("".join(current))
            current = []
        else:
            current.append(ch)
        prev = ch
    if current:
        pairs.append("".join(current))
    return pairs


# ---------------------------------------------------------------------------
# Prometheus formatting
# ---------------------------------------------------------------------------


class TestEscapeLabel:
    """Label values must be escaped for Prometheus exposition format."""

    def test_plain_string(self):
        assert _escape_label("hello") == "hello"

    def test_quotes(self):
        assert _escape_label('say "hi"') == 'say \\"hi\\"'

    def test_backslash(self):
        assert _escape_label("a\\b") == "a\\\\b"

    def test_newline(self):
        assert _escape_label("line1\nline2") == "line1\\nline2"

    def test_combined(self):
        assert _escape_label('a\\b"c\nd') == 'a\\\\b\\"c\\nd'


class TestMetricFormatting:
    """_metric produces valid Prometheus exposition blocks."""

    def test_simple_gauge(self):
        out = _metric("test_gauge", "A test", "gauge", [({}, 42)])
        assert "# HELP test_gauge A test" in out
        assert "# TYPE test_gauge gauge" in out
        assert "test_gauge 42" in out

    def test_labeled_gauge(self):
        out = _metric("m", "help", "gauge", [({"a": "1", "b": "2"}, 99)])
        assert 'm{a="1",b="2"} 99' in out

    def test_multiple_samples(self):
        out = _metric("m", "help", "gauge", [
            ({"status": "active"}, 10),
            ({"status": "disabled"}, 3),
        ])
        lines = out.splitlines()
        assert len(lines) == 4  # HELP, TYPE, sample, sample

    def test_no_samples(self):
        out = _metric("m", "help", "gauge", [])
        lines = out.splitlines()
        assert len(lines) == 2  # HELP, TYPE only

    def test_label_escaping(self):
        out = _metric("m", "help", "gauge", [({"url": 'https://x.com/a"b'}, 1)])
        assert 'url="https://x.com/a\\"b"' in out


# ---------------------------------------------------------------------------
# DB metrics collection
# ---------------------------------------------------------------------------


class FakeCursor:
    """Mock cursor that returns pre-configured results per query pattern.

    Values in the query_results dict can be:
    - A list of dicts: fetchall() returns the list, fetchone() returns [0]
    - A single dict: treated as a one-row result (wrapped in a list)
    """

    def __init__(self, query_results):
        self._results = query_results
        self._rows = []

    def execute(self, sql, args=None):
        for pattern, rows in self._results.items():
            if pattern in sql:
                if isinstance(rows, dict):
                    self._rows = [rows]
                else:
                    self._rows = rows
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass

    def commit(self):
        pass


def _make_conn(query_results):
    """Build a FakeConnection with a FakeCursor for the given query map."""
    return FakeConnection(FakeCursor(query_results))


class TestCollectDbMetrics:
    """DB metrics collection with mocked database."""

    def setup_method(self):
        _reset_caches()

    @patch("misp_container.metrics._connect")
    def test_db_unreachable(self, mock_connect):
        """misp_up=0 when the database connection fails."""
        mock_connect.side_effect = Exception("connection refused")
        output, servers = _collect_db_metrics()
        parsed = _parse_metrics(output)
        assert parsed["misp_up"][0] == ({}, "0")
        assert servers == []

    @patch("misp_container.metrics._connect")
    def test_basic_content_counts(self, mock_connect):
        """Content count metrics are emitted from information_schema + exact queries."""
        mock_connect.return_value = _make_conn({
            "system_settings": [
                {"setting": "MISP.uuid", "value": '"abc-123"'},
                {"setting": "MISP.baseurl", "value": '"https://misp.example.com"'},
            ],
            "information_schema": [
                {"TABLE_NAME": "events", "TABLE_ROWS": 500},
                {"TABLE_NAME": "attributes", "TABLE_ROWS": 15000},
                {"TABLE_NAME": "shadow_attributes", "TABLE_ROWS": 10},
            ],
            "organisations": {
                "orgs": 5, "orgs_local": 3,
                "users_active": 20, "users_disabled": 2,
                "sharing_groups": 4, "tags": 100,
            },
            "FROM servers": [],
            "FROM jobs": [],
            "misp_container_sync_log": [],
        })
        output, servers = _collect_db_metrics()
        parsed = _parse_metrics(output)

        assert parsed["misp_up"][0] == ({}, "1")
        assert parsed["misp_events"][0] == ({}, "500")
        assert parsed["misp_attributes"][0] == ({}, "15000")
        assert parsed["misp_proposals"][0] == ({}, "10")
        assert parsed["misp_organisations"][0] == ({}, "5")
        assert parsed["misp_organisations_local"][0] == ({}, "3")
        assert parsed["misp_sharing_groups"][0] == ({}, "4")
        assert parsed["misp_tags"][0] == ({}, "100")

    @patch("misp_container.metrics._connect")
    def test_instance_info(self, mock_connect):
        """Instance info metric includes uuid and base_url labels."""
        mock_connect.return_value = _make_conn({
            "system_settings": [
                {"setting": "MISP.uuid", "value": '"test-uuid"'},
                {"setting": "MISP.baseurl", "value": '"https://misp.test"'},
            ],
            "information_schema": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
            "misp_container_sync_log": [],
        })
        output, _ = _collect_db_metrics()
        parsed = _parse_metrics(output)
        labels, val = parsed["misp_instance_info"][0]
        assert labels["uuid"] == "test-uuid"
        assert labels["base_url"] == "https://misp.test"

    @patch("misp_container.metrics._connect")
    def test_user_status_labels(self, mock_connect):
        """User metrics are split by active/disabled status label."""
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "information_schema": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 8, "users_disabled": 2,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
            "misp_container_sync_log": [],
        })
        output, _ = _collect_db_metrics()
        parsed = _parse_metrics(output)
        user_samples = parsed["misp_users"]
        by_status = {s[0]["status"]: s[1] for s in user_samples}
        assert by_status["active"] == "8"
        assert by_status["disabled"] == "2"

    @patch("misp_container.metrics._connect")
    def test_server_metrics(self, mock_connect):
        """Server info, pull/push enabled, and last sync IDs are emitted."""
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "information_schema": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [
                {
                    "id": 1, "name": "Partner A", "url": "https://a.example.com",
                    "authkey": "key-a", "pull": 1, "push": 0,
                    "lastpulledid": 42, "lastpushedid": None,
                },
                {
                    "id": 2, "name": "Partner B", "url": "https://b.example.com",
                    "authkey": "key-b", "pull": 1, "push": 1,
                    "lastpulledid": 100, "lastpushedid": 50,
                },
            ],
            "FROM jobs": [],
            "misp_container_sync_log": [],
        })
        output, servers = _collect_db_metrics()
        parsed = _parse_metrics(output)

        assert len(servers) == 2
        assert len(parsed["misp_server_info"]) == 2
        assert len(parsed["misp_server_pull_enabled"]) == 2
        assert len(parsed["misp_server_push_enabled"]) == 2

        # Partner A: pull=1, push=0
        pull_by_id = {s[0]["id"]: s[1] for s in parsed["misp_server_pull_enabled"]}
        push_by_id = {s[0]["id"]: s[1] for s in parsed["misp_server_push_enabled"]}
        assert pull_by_id["1"] == "1"
        assert push_by_id["1"] == "0"
        assert pull_by_id["2"] == "1"
        assert push_by_id["2"] == "1"

        # Last pull/push IDs
        last_pull = {s[0]["id"]: s[1] for s in parsed["misp_server_last_pull_event_id"]}
        assert last_pull["1"] == "42"
        assert last_pull["2"] == "100"
        last_push = {s[0]["id"]: s[1] for s in parsed["misp_server_last_push_event_id"]}
        assert "1" not in last_push  # Partner A has no push
        assert last_push["2"] == "50"

    @patch("misp_container.metrics._connect")
    def test_no_servers(self, mock_connect):
        """No server metrics emitted when no servers are configured."""
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "information_schema": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
            "misp_container_sync_log": [],
        })
        output, servers = _collect_db_metrics()
        parsed = _parse_metrics(output)
        assert servers == []
        assert "misp_server_info" not in parsed

    @patch("misp_container.metrics._connect")
    def test_job_counters(self, mock_connect):
        """All jobs fetched in one query; split into server vs non-server counters."""
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "information_schema": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "LEFT JOIN servers": [
                # Server jobs (server_id is not None)
                {"worker": "default", "job_type": "pull", "status": 4, "server_id": 1, "server_name": "Partner A", "cnt": 8},
                {"worker": "default", "job_type": "pull", "status": 3, "server_id": 1, "server_name": "Partner A", "cnt": 1},
                {"worker": "default", "job_type": "push", "status": 4, "server_id": 2, "server_name": "Partner B", "cnt": 3},
                # Non-server jobs (server_id is None)
                {"worker": "default", "job_type": "publish", "status": 4, "server_id": None, "server_name": None, "cnt": 15},
                {"worker": "cache", "job_type": "cache_feeds", "status": 4, "server_id": None, "server_name": None, "cnt": 7},
                {"worker": "default", "job_type": "publish", "status": 3, "server_id": None, "server_name": None, "cnt": 2},
                {"worker": "prio", "job_type": "publish_event", "status": 0, "server_id": None, "server_name": None, "cnt": 4},
            ],
            "misp_container_sync_log": [],
        })
        output, _ = _collect_db_metrics()
        parsed = _parse_metrics(output)

        # Server sync job counters
        server_jobs = parsed["misp_server_jobs_total"]
        by_key = {(s[0]["id"], s[0]["job_type"], s[0]["status"]): s[1] for s in server_jobs}
        assert by_key[("1", "pull", "completed")] == "8"
        assert by_key[("1", "pull", "failed")] == "1"
        assert by_key[("2", "push", "completed")] == "3"
        names = {s[0]["id"]: s[0]["name"] for s in server_jobs}
        assert names["1"] == "Partner A"
        assert names["2"] == "Partner B"

        # Non-server job counters
        jobs = parsed["misp_jobs_total"]
        by_key = {(s[0]["worker"], s[0]["job_type"], s[0]["status"]): s[1] for s in jobs}
        assert by_key[("default", "publish", "completed")] == "15"
        assert by_key[("cache", "cache_feeds", "completed")] == "7"
        assert by_key[("default", "publish", "failed")] == "2"
        # MISP leaves a job at 0 while it waits and runs
        assert by_key[("prio", "publish_event", "unfinished")] == "4"
        assert "misp_jobs_queued" not in parsed

    @patch("misp_container.metrics._connect")
    def test_enabled_scheduled_tasks_by_type(self, mock_connect):
        mock_connect.return_value = _make_conn({
            "organisations": {"orgs": 0, "orgs_local": 0, "users_active": 0,
                              "users_disabled": 0, "sharing_groups": 0, "tags": 0},
            "FROM scheduled_tasks": [{"type": "Server", "cnt": 2}, {"type": "Feed", "cnt": 1}],
        })
        parsed = _parse_metrics(_collect_db_metrics()[0])
        enabled = {s[0]["type"]: s[1] for s in parsed["misp_scheduled_tasks_enabled"]}
        assert enabled == {"Server": "2", "Feed": "1"}

    @patch("misp_container.metrics._connect")
    def test_no_enabled_scheduled_tasks_is_zero(self, mock_connect):
        mock_connect.return_value = _make_conn({
            "organisations": {"orgs": 0, "orgs_local": 0, "users_active": 0,
                              "users_disabled": 0, "sharing_groups": 0, "tags": 0},
            "FROM scheduled_tasks": [],
        })
        parsed = _parse_metrics(_collect_db_metrics()[0])
        assert parsed["misp_scheduled_tasks_enabled"] == [({}, "0")]

    @patch("misp_container.metrics._connect")
    def test_sync_log_metrics(self, mock_connect):
        """Sync container log metrics are emitted when table exists."""
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "information_schema": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
            "GROUP BY operation, status": [
                {"operation": "org-sync", "status": "success", "runs": 5,
                 "last_run": "2026-05-15 10:00:00", "avg_duration": 2.5},
                {"operation": "org-sync", "status": "error", "runs": 1,
                 "last_run": "2026-05-15 09:00:00", "avg_duration": 1.0},
            ],
            "UNIX_TIMESTAMP": [{"operation": "org-sync", "ts": 1747310400}],
        })
        output, _ = _collect_db_metrics()
        parsed = _parse_metrics(output)

        runs = parsed["misp_sync_runs_24h"]
        by_status = {s[0]["status"]: s[1] for s in runs}
        assert by_status["success"] == "5"
        assert by_status["error"] == "1"

        assert parsed["misp_sync_last_success_timestamp_seconds"][0] == ({"operation": "org-sync"}, "1747310400")

    @patch("misp_container.metrics._connect")
    def test_sync_log_table_missing(self, mock_connect):
        """No crash when the sync log table doesn't exist yet."""
        cursor = FakeCursor({
            "system_settings": [],
            "information_schema": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
        })
        # Make sync log queries raise (table doesn't exist)
        orig_execute = cursor.execute
        def raising_execute(sql, args=None):
            if "misp_container_sync_log" in sql:
                raise Exception("Table doesn't exist")
            return orig_execute(sql, args)
        cursor.execute = raising_execute

        mock_connect.return_value = FakeConnection(cursor)
        output, _ = _collect_db_metrics()
        # Should not crash, just skip sync log metrics
        assert "misp_up" in output
        assert "misp_sync_runs_24h" not in output

    @patch("misp_container.metrics._connect")
    def test_no_db_cache(self, mock_connect):
        """Each call queries the database (no stale cache)."""
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "information_schema": [],
            "organisations": {
                "orgs": 1, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "LEFT JOIN servers": [],
            "misp_container_sync_log": [],
        })
        _collect_db_metrics()
        _collect_db_metrics()
        assert mock_connect.call_count == 2


# ---------------------------------------------------------------------------
# Network metrics collection
# ---------------------------------------------------------------------------


class TestCollectNetworkMetrics:
    """Network reachability and TLS cert checks with mocked I/O."""

    def setup_method(self):
        _reset_caches()

    def test_empty_server_list(self):
        """No output when no servers are configured."""
        output = _collect_network_metrics([])
        assert output == ""

    @patch("misp_container.metrics._check_tls_cert")
    @patch("misp_container.metrics._check_server_auth")
    def test_reachable_server(self, mock_auth, mock_tls):
        """Reachable server produces reachable=1 and cert expiry metric."""
        mock_auth.return_value = True
        mock_tls.return_value = 1750000000.0
        servers = [{"id": 1, "name": "Test", "url": "https://misp.test", "authkey": "key"}]
        output = _collect_network_metrics(servers)
        parsed = _parse_metrics(output)

        assert parsed["misp_server_reachable"][0][1] == "1"
        assert parsed["misp_server_tls_expiry_timestamp_seconds"][0][1] == "1750000000"

    @patch("misp_container.metrics._check_tls_cert")
    @patch("misp_container.metrics._check_server_auth")
    def test_unreachable_server(self, mock_auth, mock_tls):
        """Unreachable server produces reachable=0."""
        mock_auth.return_value = False
        mock_tls.return_value = None
        servers = [{"id": 1, "name": "Test", "url": "https://misp.test", "authkey": "key"}]
        output = _collect_network_metrics(servers)
        parsed = _parse_metrics(output)

        assert parsed["misp_server_reachable"][0][1] == "0"
        assert "misp_server_tls_expiry_timestamp_seconds" not in parsed

    @patch("misp_container.metrics._check_tls_cert")
    @patch("misp_container.metrics._check_server_auth")
    def test_http_server_no_tls_check(self, mock_auth, mock_tls):
        """HTTP (non-TLS) server skips certificate check."""
        mock_auth.return_value = True
        servers = [{"id": 1, "name": "Test", "url": "http://misp.test", "authkey": "key"}]
        output = _collect_network_metrics(servers)
        mock_tls.assert_not_called()
        parsed = _parse_metrics(output)
        assert "misp_server_tls_expiry_timestamp_seconds" not in parsed

    @patch("misp_container.metrics._check_tls_cert")
    @patch("misp_container.metrics._check_server_auth")
    def test_multiple_servers(self, mock_auth, mock_tls):
        """Multiple servers each get their own reachability sample."""
        mock_auth.side_effect = [True, False]
        mock_tls.side_effect = [1750000000.0, None]
        servers = [
            {"id": 1, "name": "A", "url": "https://a.test", "authkey": "k1"},
            {"id": 2, "name": "B", "url": "https://b.test", "authkey": "k2"},
        ]
        output = _collect_network_metrics(servers)
        parsed = _parse_metrics(output)

        reachable = {s[0]["id"]: s[1] for s in parsed["misp_server_reachable"]}
        assert reachable["1"] == "1"
        assert reachable["2"] == "0"

    @patch("misp_container.metrics._check_tls_cert")
    @patch("misp_container.metrics._check_server_auth")
    def test_server_with_empty_url_skipped(self, mock_auth, mock_tls):
        """Servers with no URL are skipped."""
        servers = [{"id": 1, "name": "Bad", "url": "", "authkey": "key"}]
        output = _collect_network_metrics(servers)
        mock_auth.assert_not_called()
        assert output == ""

    @patch("misp_container.metrics._check_tls_cert")
    @patch("misp_container.metrics._check_server_auth")
    def test_cache_returns_previous(self, mock_auth, mock_tls):
        """Second call within TTL returns cached output."""
        mock_auth.return_value = True
        mock_tls.return_value = 1750000000.0
        servers = [{"id": 1, "name": "A", "url": "https://a.test", "authkey": "k"}]
        out1 = _collect_network_metrics(servers)
        out2 = _collect_network_metrics(servers)
        assert out1 == out2
        assert mock_auth.call_count == 1  # second call hit cache


# ---------------------------------------------------------------------------
# Server auth check
# ---------------------------------------------------------------------------


class TestCheckServerAuth:
    """_check_server_auth probes /servers/getVersion with the authkey."""

    @patch("misp_container.metrics.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert _check_server_auth("https://misp.test", "mykey") is True

    @patch("misp_container.metrics.urllib.request.urlopen")
    def test_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("refused")
        assert _check_server_auth("https://misp.test", "mykey") is False

    @patch("misp_container.metrics.urllib.request.urlopen")
    def test_timeout(self, mock_urlopen):
        import socket
        mock_urlopen.side_effect = socket.timeout("timed out")
        assert _check_server_auth("https://misp.test", "mykey") is False


# ---------------------------------------------------------------------------
# TLS cert check
# ---------------------------------------------------------------------------


class TestCheckTlsCert:

    @patch("misp_container.metrics.socket.create_connection")
    def test_valid_cert(self, mock_conn):
        mock_ssock = MagicMock()
        mock_ssock.getpeercert.return_value = {"notAfter": "Jan  1 00:00:00 2027 GMT"}
        mock_ssock.__enter__ = MagicMock(return_value=mock_ssock)
        mock_ssock.__exit__ = MagicMock(return_value=False)

        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock

        mock_conn.return_value = mock_sock

        with patch("misp_container.metrics.ssl.create_default_context", return_value=mock_ctx):
            result = _check_tls_cert("misp.test", 443)

        assert result is not None
        assert result > 0

    @patch("misp_container.metrics.socket.create_connection")
    def test_connection_failure(self, mock_conn):
        mock_conn.side_effect = ConnectionRefusedError("refused")
        result = _check_tls_cert("misp.test", 443)
        assert result is None


# ---------------------------------------------------------------------------
# collect_all integration
# ---------------------------------------------------------------------------


class FakeSocket:
    """A socket that records what is sent and answers from scripted bytes."""

    def __init__(self, replies: bytes):
        self.sent = b""
        self._replies = io.BytesIO(replies)
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode):
        return self._replies

    def close(self):
        self.closed = True


class TestRedisClient:
    """The minimal RESP client the exporter uses for the job queues."""

    def _client(self, replies, **kwargs):
        sock = FakeSocket(replies)
        with patch("misp_container.metrics.socket.create_connection", return_value=sock):
            return _Redis("redis", 6379, **kwargs), sock

    def test_command_encoding_and_integer_reply(self):
        client, sock = self._client(b":7\r\n")
        assert client.call("LLEN", "background_jobs:default") == 7
        assert sock.sent == b"*2\r\n$4\r\nLLEN\r\n$23\r\nbackground_jobs:default\r\n"

    def test_auth_and_select_before_commands(self):
        client, sock = self._client(b"+OK\r\n+OK\r\n", password="s3cret", database=1)
        assert sock.sent == (b"*2\r\n$4\r\nAUTH\r\n$6\r\ns3cret\r\n"
                             b"*2\r\n$6\r\nSELECT\r\n$1\r\n1\r\n")

    def test_no_auth_and_no_select_for_database_zero(self):
        client, sock = self._client(b"")
        assert sock.sent == b""

    def test_error_reply_raises(self):
        client, _ = self._client(b"-NOAUTH Authentication required.\r\n")
        with pytest.raises(RedisError, match="NOAUTH"):
            client.call("LLEN", "x")

    def test_closed_connection_raises(self):
        client, _ = self._client(b"")
        with pytest.raises(RedisError, match="closed"):
            client.call("LLEN", "x")

    def test_bulk_null_and_array_replies(self):
        client, _ = self._client(b"$5\r\nhello\r\n$-1\r\n*2\r\n$1\r\na\r\n:3\r\n")
        assert client.call("GET", "k") == "hello"
        assert client.call("GET", "missing") is None
        assert client.call("X") == ["a", 3]

    def test_scan_follows_the_cursor(self):
        replies = (b"*2\r\n$2\r\n17\r\n*1\r\n$3\r\nk:1\r\n"
                   b"*2\r\n$1\r\n0\r\n*2\r\n$3\r\nk:2\r\n$3\r\nk:3\r\n")
        client, sock = self._client(replies)
        assert client.scan("k:*") == ["k:1", "k:2", "k:3"]
        assert sock.sent.count(b"SCAN") == 2
        assert b"$2\r\n17\r\n" in sock.sent


class FakeRedis:
    """Stands in for _Redis: list lengths and running keys by name."""

    instances = []

    def __init__(self, host, port, password="", database=0, timeout=3):
        self.args = (host, port, password, database)
        self.lists = {}
        self.keys = []
        self.closed = False
        FakeRedis.instances.append(self)
        self.lists.update(FakeRedis.preset_lists)
        self.keys.extend(FakeRedis.preset_keys)

    def call(self, *args):
        if args[0] == "LLEN":
            return self.lists.get(args[1], 0)
        raise AssertionError(f"unexpected command {args}")

    def scan(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in self.keys if k.startswith(prefix)]

    def close(self):
        self.closed = True


class TestCollectQueueMetrics:
    """misp_jobs_queued reads waiting and running jobs from Redis."""

    SETTINGS = {"host": "redis", "port": "6379", "password": "pw",
                "database": "1", "namespace": "background_jobs"}

    def setup_method(self):
        FakeRedis.instances = []
        FakeRedis.preset_lists = {}
        FakeRedis.preset_keys = []

    def _collect(self, settings=None):
        with patch("misp_container.metrics._job_redis_settings", return_value=settings or self.SETTINGS), \
             patch("misp_container.metrics._Redis", FakeRedis):
            return _parse_metrics(_collect_queue_metrics())

    def test_waiting_plus_running_per_queue(self):
        FakeRedis.preset_lists = {"background_jobs:default": 2, "background_jobs:prio": 1}
        FakeRedis.preset_keys = ["background_jobs:running:default:0b1c",
                                 "background_jobs:running:cache:9f2e"]
        parsed = self._collect()
        queued = {s[0]["worker"]: s[1] for s in parsed["misp_jobs_queued"]}
        assert queued == {"cache": "1", "default": "3", "email": "0", "prio": "1", "update": "0"}

    def test_connects_with_misp_settings_and_closes(self):
        self._collect()
        client = FakeRedis.instances[0]
        assert client.args == ("redis", "6379", "pw", "1")
        assert client.closed

    def test_namespace_from_settings(self):
        FakeRedis.preset_lists = {"other:default": 5}
        settings = dict(self.SETTINGS, namespace="other")
        queued = {s[0]["worker"]: s[1] for s in self._collect(settings)["misp_jobs_queued"]}
        assert queued["default"] == "5"

    def test_closes_when_a_call_fails(self):
        class Failing(FakeRedis):
            def call(self, *args):
                raise RedisError("boom")
        with patch("misp_container.metrics._job_redis_settings", return_value=self.SETTINGS), \
             patch("misp_container.metrics._Redis", Failing):
            with pytest.raises(RedisError):
                _collect_queue_metrics()
        assert FakeRedis.instances[0].closed


class TestCollectAll:
    """collect_all combines DB + network metrics with scrape metadata."""

    def setup_method(self):
        _reset_caches()
        self._queues = patch("misp_container.metrics._collect_queue_metrics",
                             return_value=_metric("misp_jobs_queued", "q", "gauge", [({"worker": "default"}, 0)]))
        self._queues.start()

    def teardown_method(self):
        self._queues.stop()

    @patch("misp_container.metrics._collect_network_metrics", return_value="")
    @patch("misp_container.metrics._collect_db_metrics")
    def test_redis_failure_sets_error_and_omits_the_queue_metric(self, mock_db, mock_net):
        """The task runner treats a missing misp_jobs_queued as unknown and dispatches."""
        mock_db.return_value = (_metric("misp_up", "up", "gauge", [({}, 1)]), [])
        with patch("misp_container.metrics._collect_queue_metrics", side_effect=OSError("refused")):
            parsed = _parse_metrics(collect_all())
        assert "misp_jobs_queued" not in parsed
        assert parsed["misp_scrape_errors"][0] == ({}, "1")

    @patch("misp_container.metrics._collect_network_metrics", return_value="")
    @patch("misp_container.metrics._collect_db_metrics")
    def test_includes_scrape_metadata(self, mock_db, mock_net):
        mock_db.return_value = (
            _metric("misp_up", "up", "gauge", [({}, 1)]),
            [],
        )
        output = collect_all()
        parsed = _parse_metrics(output)
        assert "misp_scrape_duration_seconds" in parsed
        assert "misp_scrape_errors" in parsed
        assert parsed["misp_scrape_errors"][0] == ({}, "0")

    @patch("misp_container.metrics._collect_network_metrics", return_value="")
    @patch("misp_container.metrics._collect_db_metrics")
    def test_db_failure_sets_error(self, mock_db, mock_net):
        mock_db.side_effect = RuntimeError("boom")
        output = collect_all()
        parsed = _parse_metrics(output)
        assert parsed["misp_up"][0] == ({}, "0")
        assert parsed["misp_scrape_errors"][0] == ({}, "1")

    @patch("misp_container.metrics._collect_network_metrics")
    @patch("misp_container.metrics._collect_db_metrics")
    def test_net_failure_sets_error(self, mock_db, mock_net):
        mock_db.return_value = (
            _metric("misp_up", "up", "gauge", [({}, 1)]),
            [{"id": 1, "name": "A", "url": "https://a", "authkey": "k"}],
        )
        mock_net.side_effect = RuntimeError("network boom")
        output = collect_all()
        parsed = _parse_metrics(output)
        assert parsed["misp_scrape_errors"][0] == ({}, "1")

    @patch("misp_container.metrics._collect_network_metrics", return_value="")
    @patch("misp_container.metrics._collect_db_metrics")
    def test_output_ends_with_newline(self, mock_db, mock_net):
        mock_db.return_value = (_metric("misp_up", "up", "gauge", [({}, 1)]), [])
        output = collect_all()
        assert output.endswith("\n")

    @patch("misp_container.metrics._collect_network_metrics")
    @patch("misp_container.metrics._collect_db_metrics")
    def test_passes_servers_to_network(self, mock_db, mock_net):
        """Server list from DB collection is forwarded to network checks."""
        fake_servers = [{"id": 1, "name": "A", "url": "https://a", "authkey": "k"}]
        mock_db.return_value = ("misp_up 1", fake_servers)
        mock_net.return_value = ""
        collect_all()
        mock_net.assert_called_once_with(fake_servers)


# ---------------------------------------------------------------------------
# Sync log table operations
# ---------------------------------------------------------------------------


class TestSyncLogTable:
    """init_sync_log_table and log_sync_result write to the database."""

    @patch("misp_container.metrics._connect")
    def test_init_creates_table(self, mock_connect):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = conn

        init_sync_log_table()

        calls = [str(c) for c in cursor.execute.call_args_list]
        assert any("CREATE TABLE" in c for c in calls)
        assert any("DELETE" in c and "30 DAY" in c and "operation <> 'migrate'" in c for c in calls)
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    @patch("misp_container.metrics._connect")
    def test_log_result_inserts_row(self, mock_connect):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = conn

        log_sync_result(
            operation="org-sync",
            status="success",
            summary='{"orgs": 3}',
            duration_seconds=2.5,
        )

        cursor.execute.assert_called_once()
        args = cursor.execute.call_args
        assert "INSERT INTO misp_container_sync_log" in args[0][0]
        assert args[0][1][0] == "org-sync"
        assert args[0][1][1] == "success"
        conn.commit.assert_called_once()
        conn.close.assert_called_once()


class TestCheckServerAuthEncrypted:
    """With Security.encryption_key set, servers.authkey is ciphertext the probe cannot send."""

    def _http_error(self, code):
        import urllib.error
        return urllib.error.HTTPError("https://x", code, "denied", {}, None)

    def test_plain_key_rejected_is_unreachable(self):
        with patch("misp_container.metrics.urllib.request.urlopen", side_effect=self._http_error(403)):
            assert _check_server_auth("https://x", "a" * 40) is False

    def test_encrypted_key_counts_http_answer_as_reachable(self):
        from misp_container.metrics import ENCRYPTED_MAGIC
        with patch("misp_container.metrics.urllib.request.urlopen", side_effect=self._http_error(403)):
            assert _check_server_auth("https://x", ENCRYPTED_MAGIC + "cipher") is True

    def test_encrypted_key_is_not_sent(self):
        from misp_container.metrics import ENCRYPTED_MAGIC
        seen = {}

        def fake_urlopen(req, timeout=None, context=None):
            seen["headers"] = dict(req.header_items())
            raise self._http_error(401)

        with patch("misp_container.metrics.urllib.request.urlopen", side_effect=fake_urlopen):
            _check_server_auth("https://x", ENCRYPTED_MAGIC + "cipher")
        assert "Authorization" not in seen["headers"]

    def test_bytes_authkey_from_pymysql(self):
        from misp_container.metrics import ENCRYPTED_MAGIC
        with patch("misp_container.metrics.urllib.request.urlopen", side_effect=self._http_error(403)):
            assert _check_server_auth("https://x", (ENCRYPTED_MAGIC + "cipher").encode("latin-1")) is True
            assert _check_server_auth("https://x", b"a" * 40) is False
