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
        self.returncode, output = next(self.script)
        self.stdout = iter(output.splitlines(keepends=True))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run(name, arg="", script=((0, "done\n"),), servers=()):
    """task.run with the pod preparation, the database and the console stubbed: (exit code, console calls)."""
    console = FakeConsole(script)
    with patch("misp_container.task.prepare"), \
            patch("misp_container.task.limit_worker_memory"), patch("misp_container.task.db.wait_for_db"), \
            patch("misp_container.task.db.wait_for_configure") as wait, \
            patch("misp_container.task.db.dict_query", return_value=[{"id": s} for s in servers]), \
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
        rc, calls = run("push-servers", script=[(0, "Job failed. Reason: no answer\n"), (0, "done\n")], servers=(1, 2))
        assert rc == 1 and len(calls) == 2

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
