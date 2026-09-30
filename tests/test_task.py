"""Unit tests for the periodic task runner."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import task


class FakeClient:
    def __init__(self, servers=None, indexes=None, answers=None):
        self.calls = []
        self.servers = servers or {}
        self.indexes = indexes or {}
        self.answers = answers or {}
        self.timeout = 30

    def get(self, path):
        self.calls.append(("GET", path))
        return self.indexes.get(path, {"ok": True})

    def post(self, path, data):
        self.calls.append(("POST", path))
        answer = self.answers.get(path, {"ok": True})
        if isinstance(answer, Exception):
            raise answer
        return answer

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
        assert client.calls == [("POST", "/servers/pull/1")]
        assert result["calls"] == 1

    def test_push_only_servers_with_push_enabled(self):
        client = FakeClient(servers={
            "https://a": {"id": "1", "name": "a", "push": False},
            "https://b": {"id": "2", "name": "b", "push": True},
        })
        task.run_task(client, "push-servers")
        assert client.calls == [("POST", "/servers/push/2")]

    def test_cache_servers_and_object_templates(self):
        client = FakeClient()
        task.run_task(client, "cache-servers")
        task.run_task(client, "update-object-templates")
        assert client.calls == [("POST", "/servers/cache/all"), ("POST", "/objectTemplates/update")]

    def test_push_taxii_only_enabled_servers(self):
        client = FakeClient(indexes={"/taxiiServers/index": [
            {"TaxiiServer": {"id": "1", "name": "on", "enabled": True}},
            {"TaxiiServer": {"id": "2", "name": "off", "enabled": False}},
            {"id": "3", "name": "flat", "enabled": "1"},
        ]})
        result = task.run_task(client, "push-taxii")
        assert client.calls == [("GET", "/taxiiServers/index"),
                                ("POST", "/taxiiServers/push/1"), ("POST", "/taxiiServers/push/3")]
        assert result["calls"] == 2

    def test_blueprints_execute_once_when_there_are_any(self):
        client = FakeClient(indexes={"/sharingGroupBlueprints/index": [
            {"SharingGroupBlueprint": {"id": "1"}}, {"SharingGroupBlueprint": {"id": "2"}},
        ]})
        task.run_task(client, "sharing-group-blueprints")
        assert client.calls[-1] == ("POST", "/sharingGroupBlueprints/execute")
        assert client.calls.count(("POST", "/sharingGroupBlueprints/execute")) == 1

    def test_no_blueprints_no_execute(self):
        client = FakeClient(indexes={"/sharingGroupBlueprints/index": []})
        result = task.run_task(client, "sharing-group-blueprints")
        assert client.calls == [("GET", "/sharingGroupBlueprints/index")]
        assert result == {"calls": 0, "errors": []}

    def test_workflow_by_id(self):
        client = FakeClient(answers={"/workflows/executeWorkflow/7": {"success": True, "outcome": "ok"}})
        result = task.run_task(client, "workflow", "7")
        assert client.calls == [("POST", "/workflows/executeWorkflow/7")]
        assert result == {"calls": 1, "errors": []}

    def test_failed_workflow_counts_as_no_success(self):
        client = FakeClient(answers={"/workflows/executeWorkflow/7": {"success": False, "outcome": "blocked"}})
        result = task.run_task(client, "workflow", "7")
        assert result["calls"] == 0
        assert "blocked" in result["errors"][0]

    def test_workflow_needs_a_numeric_id(self):
        with pytest.raises(ValueError):
            task.run_task(FakeClient(), "workflow", "abc")

    def test_refused_call_is_an_error(self):
        client = FakeClient(answers={"/servers/cache/all": task.APIError(405, "Method Not Allowed", "/servers/cache/all")})
        result = task.run_task(client, "cache-servers")
        assert result["calls"] == 0 and "405" in result["errors"][0]

    def test_unknown_task_raises(self):
        with pytest.raises(ValueError):
            task.run_task(FakeClient(), "no-such-task")

    def test_every_task_name_is_listed(self):
        for name in ("cache-feeds", "fetch-feeds", "cache-servers", "pull-servers", "push-servers",
                     "push-taxii", "sharing-group-blueprints", "workflow",
                     "update-galaxies", "update-taxonomies", "update-warninglists", "update-noticelists",
                     "update-object-templates", "periodic-summary", "check-user-validity",
                     "block-invalid-users"):
            assert name in task.TASKS


class TestCakeTasks:
    def _run(self, name, returncode=0):
        from subprocess import CompletedProcess
        done = CompletedProcess([], returncode, stdout="Started periodic summary\n", stderr="")
        with patch("misp_container.init.prepare") as prepare, \
                patch("misp_container.env.apply_defaults"), \
                patch("misp_container.task.subprocess.run", return_value=done) as run:
            rc = task.run_cake_task(name)
        return rc, prepare, run

    def test_renders_config_then_runs_the_console(self):
        rc, prepare, run = self._run("periodic-summary")
        prepare.assert_called_once()
        assert run.call_args[0][0][1:] == ["Server", "sendPeriodicSummaryToUsers"]
        assert rc == 0

    def test_misp_log_lines_pass_through_once(self, capsys):
        from subprocess import CompletedProcess
        misp = '{"time":"2026-09-30T08:06:18.647Z","level":"info","context":"misp","message":"OIDC user alice"}'
        done = CompletedProcess([], 0, stdout=f"{misp}\nalice@example.com: valid\n", stderr=None)
        with patch("misp_container.init.prepare"), patch("misp_container.env.apply_defaults"), \
                patch("misp_container.task.subprocess.run", return_value=done), \
                patch.object(task.log, "info") as info:
            task.run_cake_task("check-user-validity")
        assert capsys.readouterr().out.splitlines() == [misp]
        assert [c.args[1] for c in info.call_args_list[1:]] == ["alice@example.com: valid"]

    def test_user_validity_variants(self):
        _, _, run = self._run("check-user-validity")
        assert run.call_args[0][0][1:] == ["Admin", "checkUserValidity"]
        _, _, run = self._run("block-invalid-users")
        assert run.call_args[0][0][1:] == ["Admin", "blockInvalidUsers"]

    def test_console_failure_exits_1_without_an_admin_key(self):
        with patch.dict(os.environ, {"ADMIN_KEY": ""}), \
                patch("misp_container.task.run_cake_task", return_value=255):
            with pytest.raises(SystemExit) as exc:
                task.main(["check-user-validity"])
        assert exc.value.code == 1

    def test_console_success_exits_0(self):
        with patch("misp_container.task.run_cake_task", return_value=0):
            with pytest.raises(SystemExit) as exc:
                task.main(["periodic-summary"])
        assert exc.value.code == 0


class TestMain:
    def test_missing_admin_key_exits_1(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "", "SYNC_BASE_URL": "http://web:8080"}):
            with pytest.raises(SystemExit) as exc:
                task.main(["update-galaxies"])
        assert exc.value.code == 1

    def test_every_task_has_a_description(self):
        assert set(task.DESCRIPTIONS) == set(task.TASKS)

    def test_no_task_argument_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            task.main([])
        assert exc.value.code == 2

    def test_workflow_without_an_id_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            task.main(["workflow"])
        assert exc.value.code == 2

    def test_extra_argument_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            task.main(["update-galaxies", "7"])
        assert exc.value.code == 2


class TestBacklogGate:
    METRICS = "misp_jobs_queued{worker=\"default\"} 150\nmisp_jobs_queued{worker=\"prio\"} 70\nother 1\n"

    def test_queued_jobs_sums_the_gauge(self):
        import io
        with patch("urllib.request.urlopen", return_value=io.BytesIO(self.METRICS.encode())):
            assert task.queued_jobs("http://m/metrics") == 220

    def test_unreachable_exporter_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert task.queued_jobs("http://m/metrics") is None

    def test_backlog_skips_dispatch(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "k" * 40, "SYNC_BASE_URL": "http://web:8080", "TASK_MAX_QUEUED": "100"}), \
                patch("misp_container.task.queued_jobs", return_value=150), \
                patch("misp_container.task.run_task") as run:
            with pytest.raises(SystemExit) as exc:
                task.main(["update-galaxies"])
        assert exc.value.code == 0
        run.assert_not_called()

    def test_below_limit_dispatches(self):
        with patch.dict(os.environ, {"ADMIN_KEY": "k" * 40, "SYNC_BASE_URL": "http://web:8080", "TASK_MAX_QUEUED": "100"}), \
                patch("misp_container.task.queued_jobs", return_value=10), \
                patch("misp_container.task.run_task", return_value={"calls": 1, "errors": []}) as run:
            with pytest.raises(SystemExit) as exc:
                task.main(["update-galaxies"])
        assert exc.value.code == 0
        run.assert_called_once()
