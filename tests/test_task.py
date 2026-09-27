"""Unit tests for the periodic task runner."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import task


class FakeClient:
    def __init__(self, servers=None):
        self.calls = []
        self.servers = servers or {}
        self.timeout = 30

    def get(self, path):
        self.calls.append(("GET", path))
        return {"ok": True}

    def post(self, path, data):
        self.calls.append(("POST", path))
        return {"ok": True}

    def get_servers(self):
        return self.servers


class TestRunTask:
    def test_simple_endpoint(self):
        client = FakeClient()
        result = task.run_task(client, "update-galaxies")
        assert client.calls == [("POST", "/galaxies/update")]
        assert result["calls"] == 1

    def test_pull_only_servers_with_pull_enabled(self):
        client = FakeClient(servers={
            "https://a": {"id": "1", "name": "a", "pull": True},
            "https://b": {"id": "2", "name": "b", "pull": False},
        })
        result = task.run_task(client, "pull-servers")
        assert client.calls == [("GET", "/servers/pull/1")]
        assert result["calls"] == 1

    def test_push_only_servers_with_push_enabled(self):
        client = FakeClient(servers={
            "https://a": {"id": "1", "name": "a", "push": False},
            "https://b": {"id": "2", "name": "b", "push": True},
        })
        task.run_task(client, "push-servers")
        assert client.calls == [("GET", "/servers/push/2")]

    def test_unknown_task_raises(self):
        with pytest.raises(ValueError):
            task.run_task(FakeClient(), "no-such-task")

    def test_every_task_name_is_listed(self):
        for name in ("cache-feeds", "fetch-feeds", "pull-servers", "push-servers",
                     "update-galaxies", "update-taxonomies", "update-warninglists", "update-noticelists"):
            assert name in task.TASKS


class TestMain:
    def test_missing_admin_key_exits_1(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "", "SYNC_BASE_URL": "http://web:8080"}):
            with pytest.raises(SystemExit) as exc:
                task.main(["update-galaxies"])
        assert exc.value.code == 1

    def test_no_task_argument_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            task.main([])
        assert exc.value.code == 2
