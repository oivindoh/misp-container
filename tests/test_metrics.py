"""Unit tests for the Prometheus metrics exporter."""

import base64
import os
import ssl
import sys
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import redis
from prometheus_client.parser import text_string_to_metric_families

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container.metrics import (
    ENCRYPTED_MAGIC,
    _escape_label,
    _metric,
    _collect_db_metrics,
    _collect_network_metrics,
    _collect_queue_metrics,
    _check_partner,
    _partner_tls,
    _proxy,
    collect_all,
    network_metrics,
    refresh_network_metrics,
)
import misp_container.metrics as metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_caches():
    """Forget the last partner check block."""
    metrics._net_output = ""


def _parse_metrics(text):
    """{sample name: [(labels, value as the exposition writes it)]}, read by the reference parser."""
    result = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            value = float(sample.value)
            result.setdefault(sample.name, []).append(
                (sample.labels, str(int(value)) if value.is_integer() else str(value)))
    return result


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

    def close(self):
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

    @pytest.fixture(autouse=True)
    def no_manual_settings(self):
        with patch("misp_container.metrics.db.manual_settings", return_value=[]) as manual:
            yield manual

    def setup_method(self):
        _reset_caches()

    @patch("misp_container.metrics._connect")
    def test_manual_settings(self, mock_connect, no_manual_settings):
        """Each setting changed outside the repository is a series, with their count."""
        no_manual_settings.return_value = [{"setting": "MISP.welcome_text_top", "changed": ""}]
        mock_connect.return_value = _make_conn({"FROM servers": [], "FROM jobs": []})
        parsed = _parse_metrics(_collect_db_metrics()[0])
        assert parsed["misp_settings_manual"] == [({}, "1")]
        assert parsed["misp_setting_manual"] == [({"setting": "MISP.welcome_text_top"}, "1")]

    @patch("misp_container.metrics._connect")
    def test_db_unreachable(self, mock_connect):
        """misp_up=0 when the database connection fails."""
        mock_connect.side_effect = Exception("connection refused")
        output, errors = _collect_db_metrics()
        parsed = _parse_metrics(output)
        assert parsed["misp_up"][0] == ({}, "0")
        assert errors == 1

    @patch("misp_container.metrics._connect")
    def test_basic_content_counts(self, mock_connect):
        """Content count metrics are emitted from pg_stat_user_tables + exact queries."""
        mock_connect.return_value = _make_conn({
            "system_settings": [
                {"setting": "MISP.uuid", "value": '"abc-123"'},
                {"setting": "MISP.baseurl", "value": '"https://misp.example.com"'},
            ],
            "pg_stat_user_tables": [
                {"relname": "events", "live_rows": 500},
                {"relname": "attributes", "live_rows": 15000},
                {"relname": "shadow_attributes", "live_rows": 10},
            ],
            "organisations": {
                "orgs": 5, "orgs_local": 3,
                "users_active": 20, "users_disabled": 2,
                "sharing_groups": 4, "tags": 100,
            },
            "FROM servers": [],
            "FROM jobs": [],
        })
        output, errors = _collect_db_metrics()
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
            "pg_stat_user_tables": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
        })
        output, _ = _collect_db_metrics()
        parsed = _parse_metrics(output)
        labels, val = parsed["misp_instance_info"][0]
        assert labels["uuid"] == "test-uuid"
        assert labels["base_url"] == "https://misp.test"

    @patch("misp_container.metrics._connect")
    def test_instance_info_from_the_env_layer(self, mock_connect, monkeypatch):
        monkeypatch.setenv("MISP_SETTING_MISP__uuid", "env-uuid")
        monkeypatch.setenv("MISP_SETTING_MISP__baseurl", "https://env.test")
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "pg_stat_user_tables": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
        })
        labels, _ = _parse_metrics(_collect_db_metrics()[0])["misp_instance_info"][0]
        assert (labels["uuid"], labels["base_url"]) == ("env-uuid", "https://env.test")

    @patch("misp_container.metrics._connect")
    def test_user_status_labels(self, mock_connect):
        """User metrics are split by active/disabled status label."""
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "pg_stat_user_tables": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 8, "users_disabled": 2,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
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
            "pg_stat_user_tables": [],
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
        })
        output, errors = _collect_db_metrics()
        parsed = _parse_metrics(output)

        assert errors == 0
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
            "pg_stat_user_tables": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
        })
        output, errors = _collect_db_metrics()
        parsed = _parse_metrics(output)
        assert errors == 0
        assert "misp_server_info" not in parsed

    @patch("misp_container.metrics._connect")
    def test_jobs_and_the_last_sync_jobs_per_server(self, mock_connect):
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "pg_stat_user_tables": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs GROUP BY": [
                {"worker": "default", "job_type": "pull", "status": 4, "cnt": 8},
                {"worker": "default", "job_type": "publish", "status": 3, "cnt": 2},
                {"worker": "prio", "job_type": "publish_event", "status": 0, "cnt": 4},
            ],
            "JOIN servers": [
                {"id": 1, "name": "Partner A", "job_type": "pull", "status": 4, "last": 1761480000.0},
                {"id": 1, "name": "Partner A", "job_type": "pull", "status": 3, "last": 1761483600.0},
            ],
        })
        parsed = _parse_metrics(_collect_db_metrics()[0])

        jobs = {(l["worker"], l["job_type"], l["status"]): v for l, v in parsed["misp_jobs"]}
        # MISP leaves a job at 0 while it waits and runs
        assert jobs == {("default", "pull", "completed"): "8", ("default", "publish", "failed"): "2",
                        ("prio", "publish_event", "unfinished"): "4"}
        last = {(l["id"], l["name"], l["job_type"], l["status"]): v
                for l, v in parsed["misp_server_job_last_timestamp_seconds"]}
        assert last == {("1", "Partner A", "pull", "completed"): "1761480000",
                        ("1", "Partner A", "pull", "failed"): "1761483600"}
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
    def test_job_run_metrics(self, mock_connect):
        """Each container job's row of system_settings becomes a run counter and a last-success time."""
        mock_connect.return_value = _make_conn({
            "setting LIKE": [
                {"setting": "misp_docker.runs.org-sync",
                 "value": b'{"success": 5, "error": 1, "last_success": 1747310400, "last_error": 1747300000}'},
                {"setting": "misp_docker.runs.configure", "value": '{"error": 2, "last_error": 1747300000}'},
            ],
            "system_settings": [],
            "pg_stat_user_tables": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
        })
        parsed = _parse_metrics(_collect_db_metrics()[0])
        runs = {(s[0]["operation"], s[0]["status"]): s[1] for s in parsed["misp_container_job_runs_total"]}
        assert runs == {("configure", "error"): "2", ("configure", "success"): "0",
                        ("org-sync", "error"): "1", ("org-sync", "success"): "5"}
        assert parsed["misp_container_job_last_success_timestamp_seconds"] == [
            ({"operation": "org-sync"}, "1747310400")]

    @patch("misp_container.metrics._connect")
    def test_job_runs_query_failure(self, mock_connect):
        """A failed read of the run records counts as an error and drops only those metrics."""
        cursor = FakeCursor({
            "system_settings": [],
            "pg_stat_user_tables": [],
            "organisations": {
                "orgs": 0, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "FROM jobs": [],
        })
        orig_execute = cursor.execute
        def raising_execute(sql, args=None):
            if "setting LIKE" in sql:
                raise Exception("connection lost")
            return orig_execute(sql, args)
        cursor.execute = raising_execute

        mock_connect.return_value = FakeConnection(cursor)
        output, errors = _collect_db_metrics()
        assert "misp_up" in output
        assert "misp_container_job_runs_total" not in output
        assert errors == 1

    @patch("misp_container.metrics._connect")
    def test_no_db_cache(self, mock_connect):
        """Each call queries the database (no stale cache)."""
        mock_connect.return_value = _make_conn({
            "system_settings": [],
            "pg_stat_user_tables": [],
            "organisations": {
                "orgs": 1, "orgs_local": 0,
                "users_active": 0, "users_disabled": 0,
                "sharing_groups": 0, "tags": 0,
            },
            "FROM servers": [],
            "LEFT JOIN servers": [],
        })
        _collect_db_metrics()
        _collect_db_metrics()
        assert mock_connect.call_count == 2


# ---------------------------------------------------------------------------
# Network metrics collection
# ---------------------------------------------------------------------------


class TestCollectNetworkMetrics:
    """The partner block from _check_partner's answers."""

    def setup_method(self):
        _reset_caches()

    def test_empty_server_list(self):
        assert _collect_network_metrics([]) == ""

    @patch("misp_container.metrics._check_partner", return_value=(True, 1750000000.0))
    def test_reachable_server(self, mock_check):
        servers = [{"id": 1, "name": "Test", "url": "https://misp.test", "authkey": "key"}]
        parsed = _parse_metrics(_collect_network_metrics(servers))
        assert parsed["misp_server_reachable"][0][1] == "1"
        assert parsed["misp_server_tls_expiry_timestamp_seconds"][0][1] == "1750000000"

    @patch("misp_container.metrics._check_partner", return_value=(False, None))
    def test_unreachable_server(self, mock_check):
        servers = [{"id": 1, "name": "Test", "url": "https://misp.test", "authkey": "key"}]
        parsed = _parse_metrics(_collect_network_metrics(servers))
        assert parsed["misp_server_reachable"][0][1] == "0"
        assert "misp_server_tls_expiry_timestamp_seconds" not in parsed

    @patch("misp_container.metrics._check_partner", side_effect=[(True, 1750000000.0), (False, None)])
    def test_multiple_servers(self, mock_check):
        servers = [
            {"id": 1, "name": "A", "url": "https://a.test", "authkey": "k1"},
            {"id": 2, "name": "B", "url": "https://b.test", "authkey": "k2"},
        ]
        parsed = _parse_metrics(_collect_network_metrics(servers))
        assert {l["id"]: v for l, v in parsed["misp_server_reachable"]} == {"1": "1", "2": "0"}

    @patch("misp_container.metrics._check_partner")
    def test_server_with_empty_url_skipped(self, mock_check):
        assert _collect_network_metrics([{"id": 1, "name": "Bad", "url": "", "authkey": "key"}]) == ""
        mock_check.assert_not_called()

    @patch("misp_container.metrics._check_partner", return_value=(True, 1750000000.0))
    @patch("misp_container.metrics._connect")
    def test_refresh_stores_the_block_for_the_scrape(self, mock_connect, mock_check):
        """The check thread reads the servers itself and leaves the block for collect_all."""
        mock_connect.return_value = _make_conn({"FROM servers": [
            {"id": 1, "name": "A", "url": "https://a.test", "authkey": "k"}]})
        refresh_network_metrics()
        assert 'misp_server_reachable{id="1",name="A",url="https://a.test"} 1' in network_metrics()

    @patch("misp_container.metrics._connect", side_effect=OSError("refused"))
    def test_a_failed_refresh_keeps_the_last_block(self, mock_connect):
        metrics._net_output = "misp_server_reachable 1"
        refresh_network_metrics()
        assert network_metrics() == "misp_server_reachable 1"


# ---------------------------------------------------------------------------
# The partner check, connected as MISP connects
# ---------------------------------------------------------------------------

# A self-signed certificate for misp.test that expires 2036-10-23 13:00:51 UTC
PARTNER_DER = base64.b64decode(
    "MIIBWzCCAQKgAwIBAgIUVPqfBYDuBpb+YlS2lkeSsbsQS9MwCgYIKoZIzj0EAwIwFDESMBAGA1UEAwwJbWlzcC50ZXN0MB4XDTI2"
    "MTAyNjEzMDA1MVoXDTM2MTAyMzEzMDA1MVowFDESMBAGA1UEAwwJbWlzcC50ZXN0MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE"
    "33g7X3foNXzMtdg5l7IC3hXNWZ+XMSOaaz0sIybTeXUXjnIxaIozdKMz4hshsk1Q8DN9uEitNBeUoRTwya0bqqMyMDAwHQYDVR0O"
    "BBYEFPtq6OemKROPidXPUQeZlm76BT8WMA8GA1UdEwEB/wQFMAMBAf8wCgYIKoZIzj0EAwIDRwAwRAIgI2oTjzB/YAjW+wB8A4LW"
    "T6uGGRtkR2EM9XgHCs/vh/UCIG915f0V1phyhHCTn+L0bSeJwTFnCjPxBwjjwscgUp0F")
PARTNER_EXPIRY = 2108379651


class FakeHTTP:
    """Stands in for an http.client connection class: records each connection, answers with a status."""

    def __init__(self, status=200, fail=None):
        self.status, self.fail = status, fail
        self.made = []

    def __call__(self, host, port, timeout=None, context=None):
        conn = MagicMock()
        conn.target = (host, port)
        conn.context = context
        if self.fail:
            conn.connect.side_effect = self.fail
        conn.sock.getpeercert.return_value = PARTNER_DER
        conn.getresponse.return_value.status = self.status
        self.made.append(conn)
        return conn


def check(server, proxy=None, status=200, fail=None):
    """_check_partner against FakeHTTP connections: its answer and the one connection made."""
    fake = FakeHTTP(status, fail)
    with patch("misp_container.metrics.http.client.HTTPSConnection", fake), \
            patch("misp_container.metrics.http.client.HTTPConnection", fake), \
            patch("misp_container.metrics._partner_tls", return_value="tls"):
        answer = _check_partner({"id": 1, "authkey": "a" * 40, **server}, proxy)
    return answer, fake.made[0]


PROXY = {"host": "proxy", "port": 3128, "headers": {"Proxy-Authorization": "Basic dTpw"}}


class TestCheckPartner:
    def test_a_partner_that_answers_is_reachable_with_its_expiry(self):
        answer, conn = check({"url": "https://misp.test/"})
        assert answer == (True, PARTNER_EXPIRY)
        assert conn.target == ("misp.test", 443) and conn.context == "tls"
        conn.request.assert_called_once_with("GET", "/servers/getVersion",
                                             headers={"Accept": "application/json", "Authorization": "a" * 40})

    def test_a_refused_connection_is_unreachable(self):
        assert check({"url": "https://misp.test"}, fail=ConnectionRefusedError())[0] == (False, None)

    def test_a_rejected_key_is_unreachable(self):
        assert check({"url": "https://misp.test"}, status=403)[0][0] is False

    @pytest.mark.parametrize("authkey", [ENCRYPTED_MAGIC + "cipher", (ENCRYPTED_MAGIC + "cipher").encode("latin-1")])
    def test_an_encrypted_key_is_not_sent_and_any_answer_counts(self, authkey):
        # With Security.encryption_key set, servers.authkey is ciphertext the check cannot send
        answer, conn = check({"url": "https://misp.test", "authkey": authkey}, status=403)
        assert answer[0] is True
        assert "Authorization" not in conn.request.call_args.kwargs["headers"]

    def test_an_http_partner_has_no_expiry(self):
        answer, conn = check({"url": "http://misp.test:8080"})
        assert answer == (True, None) and conn.target == ("misp.test", 8080)

    def test_the_proxy_tunnels_to_the_partner(self):
        _, conn = check({"url": "https://misp.test"}, PROXY)
        assert conn.target == ("proxy", 3128)
        conn.set_tunnel.assert_called_once_with("misp.test", 443, headers=PROXY["headers"])

    def test_skip_proxy_connects_directly(self):
        _, conn = check({"url": "https://misp.test", "skip_proxy": True}, PROXY)
        assert conn.target == ("misp.test", 443)
        conn.set_tunnel.assert_not_called()

    def test_an_http_partner_through_the_proxy_takes_the_absolute_url(self):
        _, conn = check({"url": "http://misp.test"}, PROXY)
        assert conn.target == ("proxy", 3128)
        assert conn.request.call_args.args[1] == "http://misp.test:80/servers/getVersion"
        assert conn.request.call_args.kwargs["headers"]["Proxy-Authorization"] == "Basic dTpw"


class TestPartnerTls:
    def tls(self, server):
        ctx = MagicMock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        with patch("misp_container.metrics.ssl.create_default_context", return_value=ctx) as create, \
                patch.dict(os.environ, {"MISP_SETTING_MISP__ca_path": "/etc/ssl/certs/ca-certificates.crt"}):
            assert _partner_tls(server) is ctx
        return ctx, create.call_args.kwargs["cafile"]

    def test_the_ca_path_of_misp_by_default(self):
        ctx, cafile = self.tls({})
        assert cafile == "/etc/ssl/certs/ca-certificates.crt"
        assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED

    def test_self_signed_without_a_pinned_certificate_checks_none(self):
        ctx, _ = self.tls({"self_signed": True})
        assert not ctx.check_hostname and ctx.verify_mode == ssl.CERT_NONE

    def test_a_pinned_certificate_from_the_secret(self):
        ctx, cafile = self.tls({"self_signed": True, "cert_file": "3.pem"})
        assert cafile == "/etc/misp-docker/certs/3.pem"
        assert not ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED

    def test_a_client_certificate_from_the_secret(self):
        ctx, _ = self.tls({"client_cert_file": "3_client.pem"})
        ctx.load_cert_chain.assert_called_once_with("/etc/misp-docker/certs/3_client.pem")


class TestProxy:
    def test_no_host_no_proxy(self):
        with patch.dict(os.environ, {"MISP_SETTING_Proxy__host": ""}):
            assert _proxy() is None

    def test_misps_proxy_settings(self):
        with patch.dict(os.environ, {"MISP_SETTING_Proxy__host": "proxy", "MISP_SETTING_Proxy__port": "",
                                     "MISP_SETTING_Proxy__user": "u", "MISP_SETTING_Proxy__password": "p"}):
            assert _proxy() == PROXY


# ---------------------------------------------------------------------------
# collect_all integration
# ---------------------------------------------------------------------------


class FakeRedis:
    """Stands in for redis.Redis: list lengths and running keys by name."""

    instances = []
    preset_lists = {}
    preset_keys = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        FakeRedis.instances.append(self)

    def llen(self, name):
        return FakeRedis.preset_lists.get(name, 0)

    def scan_iter(self, match, count):
        prefix = match.rstrip("*")
        return iter([k for k in FakeRedis.preset_keys if k.startswith(prefix)])

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

    def _collect(self, settings=None, client=FakeRedis):
        with patch("misp_container.metrics._job_redis_settings", return_value=settings or self.SETTINGS), \
             patch("misp_container.metrics.redis.Redis", client):
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
        assert {k: client.kwargs[k] for k in ("host", "port", "password", "db", "ssl")} == \
            {"host": "redis", "port": 6379, "password": "pw", "db": 1, "ssl": False}
        assert client.closed

    def test_a_tls_host_connects_over_tls(self):
        self._collect(dict(self.SETTINGS, host="tls://redis.example"))
        assert FakeRedis.instances[0].kwargs["host"] == "redis.example"
        assert FakeRedis.instances[0].kwargs["ssl"] is True

    def test_namespace_from_settings(self):
        FakeRedis.preset_lists = {"other:default": 5}
        settings = dict(self.SETTINGS, namespace="other")
        queued = {s[0]["worker"]: s[1] for s in self._collect(settings)["misp_jobs_queued"]}
        assert queued["default"] == "5"

    def test_closes_when_a_call_fails(self):
        class Failing(FakeRedis):
            def llen(self, name):
                raise redis.ConnectionError("boom")
        with pytest.raises(redis.ConnectionError):
            self._collect(client=Failing)
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

    @patch("misp_container.metrics._collect_db_metrics")
    def test_redis_failure_sets_error_and_omits_the_queue_metric(self, mock_db):
        """The task runner treats a missing misp_jobs_queued as unknown and dispatches."""
        mock_db.return_value = (_metric("misp_up", "up", "gauge", [({}, 1)]), 0)
        with patch("misp_container.metrics._collect_queue_metrics", side_effect=OSError("refused")):
            parsed = _parse_metrics(collect_all())
        assert "misp_jobs_queued" not in parsed
        assert parsed["misp_scrape_errors"][0] == ({}, "1")

    @patch("misp_container.metrics._collect_db_metrics")
    def test_includes_scrape_metadata(self, mock_db):
        mock_db.return_value = (_metric("misp_up", "up", "gauge", [({}, 1)]), 0)
        output = collect_all()
        parsed = _parse_metrics(output)
        assert "misp_scrape_duration_seconds" in parsed
        assert "misp_scrape_errors" in parsed
        assert parsed["misp_scrape_errors"][0] == ({}, "0")

    @patch("misp_container.metrics._collect_db_metrics")
    def test_db_failure_sets_error(self, mock_db):
        mock_db.side_effect = RuntimeError("boom")
        output = collect_all()
        parsed = _parse_metrics(output)
        assert parsed["misp_up"][0] == ({}, "0")
        assert parsed["misp_scrape_errors"][0] == ({}, "1")

    @patch("misp_container.metrics._collect_db_metrics")
    def test_a_failed_collector_counts_as_an_error(self, mock_db):
        mock_db.return_value = (_metric("misp_up", "up", "gauge", [({}, 1)]), 2)
        parsed = _parse_metrics(collect_all())
        assert parsed["misp_scrape_errors"][0] == ({}, "2")

    @patch("misp_container.metrics._collect_db_metrics")
    def test_output_ends_with_newline(self, mock_db):
        mock_db.return_value = (_metric("misp_up", "up", "gauge", [({}, 1)]), 0)
        output = collect_all()
        assert output.endswith("\n")

    @patch("misp_container.metrics._collect_db_metrics")
    def test_serves_the_last_partner_block(self, mock_db):
        """The scrape includes what the check thread left, and never runs a check itself."""
        mock_db.return_value = ("misp_up 1", 0)
        metrics._net_output = _metric("misp_server_reachable", "r", "gauge", [({"id": "1"}, 1)])
        with patch("misp_container.metrics._check_partner") as check:
            parsed = _parse_metrics(collect_all())
        check.assert_not_called()
        assert parsed["misp_server_reachable"][0] == ({"id": "1"}, "1")
