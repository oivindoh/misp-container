"""Unit tests for the periodic task runner."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import CAKE, task


class FakeConsole:
    """A stand-in for subprocess.Popen: each call takes the next (exit code, output) of the script."""

    def __init__(self, script):
        self.script = iter(script)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        self.args = args
        self.returncode, output = next(self.script)
        self.stdout = iter(output.splitlines(keepends=True)) if isinstance(output, str) else output(self)
        return self

    def poll(self):
        return None

    def terminate(self):
        self.returncode = -15

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_db(servers, jobs):
    """dict_query for the server list, the newest job ID, and each server's job row (a status, or None for none)."""
    statuses = iter(jobs if jobs is not None else [task.JOB_COMPLETED] * len(servers))

    def dict_query(sql, params=None):
        if sql.startswith("SELECT id FROM servers"):
            return [{"id": s} for s in servers]
        if "MAX(id)" in sql:
            return [{"id": 0}]
        status = next(statuses)
        return [] if status is None else [{"status": status, "message": "Job failed. Reason: no answer"}]
    return dict_query


def run(name, arg="", script=((0, "done\n"),), servers=(), jobs=None):
    """task.run with the pod preparation, the database and the console stubbed: (exit code, console calls)."""
    console = FakeConsole(script)
    task.Stop.requested, task.Stop.proc = False, None
    with patch("misp_container.task.prepare"), \
            patch("misp_container.task.limit_worker_memory"), patch("misp_container.task.db.wait_for_db"), \
            patch("misp_container.task.db.wait_for_configure") as wait, \
            patch("misp_container.task.db.dict_query", fake_db(servers, jobs)), \
            patch("misp_container.task.signal.signal"), patch("misp_container.task.Relay"), \
            patch("misp_container.task.subprocess.Popen", console):
        rc = task.run(name, arg)
    wait.assert_called_once()
    return rc, console.calls


class TestTasks:
    def test_a_workflow_takes_its_id(self):
        assert task.command("workflow", "7") == [CAKE, "Workflow", "executeAdHocWorkflow", "7"]

    def test_every_task_has_a_description(self):
        assert set(task.DESCRIPTIONS) == set(task.TASKS)

    def test_the_scheduler_coverage_names_tasks(self):
        assert set(task.SCHEDULER_COVERAGE.values()) <= set(task.TASKS)


class TestRun:
    def test_waits_for_configure_then_runs_the_console_in_the_pod(self):
        assert run("update-taxonomies") == (0, [[CAKE, "Admin", "updateTaxonomies"]])

    def test_a_failing_console_fails_the_run(self):
        assert run("update-taxonomies", script=[(255, "")])[0] == 1

    def test_a_server_task_runs_once_per_server_in_the_pod(self):
        rc, calls = run("pull-servers", script=[(0, "Pull completed.\n")] * 2, servers=(3, 1))
        assert rc == 0
        assert calls == [[CAKE, "Server", "pull", "1", "3", "full"], [CAKE, "Server", "pull", "1", "1", "full"]]

    def test_a_server_that_fails_fails_the_run_after_the_others(self):
        # Push prints nothing and exits 0: only its job row says that it failed
        rc, calls = run("push-servers", script=[(0, ""), (0, "")], servers=(1, 2), jobs=[3, task.JOB_COMPLETED])
        assert rc == 1 and len(calls) == 2

    def test_a_server_without_its_job_row_fails_the_run(self):
        assert run("push-servers", script=[(0, "")], servers=(1,), jobs=[None])[0] == 1

    @pytest.mark.parametrize("name, output, rc", [
        ("fetch-feeds", "Job done. 0 feeds pulled successfully, 1 feeds could not be pulled.", 1),
        ("fetch-feeds", "Job done. 2 feeds pulled successfully, 0 feeds could not be pulled.", 0),
        ("cache-feeds", "0 feeds from 1 cached. Failed: 1 See error logs for more details.", 1),
        ("cache-feeds", "1 feed from 1 cached. Failed: 0", 0),
        ("cache-feeds", "Job failed. See error logs for more details.", 1),
    ])
    def test_a_feed_task_fails_on_a_feed_it_could_not_get(self, name, output, rc):
        # MISP saves these runs as completed and exits 0
        assert run(name, script=[(0, output + "\n")])[0] == rc

    def test_sigterm_stops_the_running_command_and_the_servers_after_it(self):
        def output(proc):
            yield "Pulling\n"
            task.Stop.handle()
            assert proc.returncode == -15
        rc, calls = run("pull-servers", script=[(0, output)], servers=(1, 2), jobs=[3])
        assert rc == 1 and len(calls) == 1

    def test_a_pull_error_fails_the_run(self):
        assert run("pull-servers", script=[(0, "ERROR: curl error 7\n")], servers=(1,))[0] == 1

    def test_no_server_runs_nothing(self):
        assert run("cache-servers", servers=()) == (0, [])

    def test_misp_log_lines_pass_through_once(self, capsys):
        misp = '{"time":"2026-09-30T08:06:18.647Z","level":"info","context":"misp","message":"OIDC user alice"}'
        with patch.object(task.log, "info") as info:
            run("check-user-validity", script=[(0, f"{misp}\nalice@example.com: valid\n")])
        assert capsys.readouterr().out.splitlines() == [misp]
        assert [c.args[1] for c in info.call_args_list[1:]] == ["alice@example.com: valid"]


class TestMain:
    def test_a_failing_run_exits_1(self):
        with patch("misp_container.task.run", return_value=1):
            with pytest.raises(SystemExit) as exc:
                task.main(["check-user-validity"])
        assert exc.value.code == 1

    def test_success_exits_0(self):
        with patch("misp_container.task.run", return_value=0):
            with pytest.raises(SystemExit) as exc:
                task.main(["pull-servers"])
        assert exc.value.code == 0

    @pytest.mark.parametrize("argv", [[], ["workflow"], ["workflow", "seven"], ["update-galaxies", "7"], ["no-such"]])
    def test_a_wrong_call_exits_2(self, argv):
        with pytest.raises(SystemExit) as exc:
            task.main(argv)
        assert exc.value.code == 2
