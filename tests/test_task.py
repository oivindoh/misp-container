"""Unit tests for the periodic task runner."""

import os
import sys
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import CAKE, task


def run(name, arg="", returncode=0, stdout="done\n", blueprints="1"):
    """task.run with the pod preparation and the console stubbed: (exit code, the console call or None)."""
    done = CompletedProcess([], returncode, stdout=stdout, stderr=None)
    with patch("misp_container.task.prepare"), patch("misp_container.task.apply_defaults"), \
            patch("misp_container.task.limit_worker_memory"), \
            patch("misp_container.task.db.query", return_value=blueprints), \
            patch("misp_container.task.subprocess.run", return_value=done) as console:
        rc = task.run(name, arg)
    return rc, (console.call_args[0][0] if console.called else None)


class TestCommands:
    @pytest.mark.parametrize("name, line", [
        ("pull-servers", ["Server", "pullAll", "1", "full"]),
        ("push-servers", ["Server", "pushAll", "1", "full"]),
        ("cache-servers", ["Server", "cacheServerAll", "1"]),
        ("fetch-feeds", ["Server", "fetchFeed", "1", "all"]),
        ("cache-feeds", ["Server", "cacheFeed", "1", "all"]),
        ("push-taxii", ["Server", "push_taxii", "1", "all"]),
        ("update-galaxies", ["Admin", "updateGalaxies", "false"]),
        ("update-object-templates", ["Admin", "updateObjectTemplates", "1"]),
        ("sharing-group-blueprints", ["Admin", "executeSGBlueprint", "all"]),
        ("periodic-summary", ["Server", "sendPeriodicSummaryToUsers"]),
        ("check-user-validity", ["Admin", "checkUserValidity"]),
    ])
    def test_the_command_line_misps_scheduler_builds(self, name, line):
        assert task.command(name) == [CAKE, *line]

    def test_a_workflow_takes_its_id(self):
        assert task.command("workflow", "7") == [CAKE, "Workflow", "executeAdHocWorkflow", "7"]

    def test_every_task_has_a_description(self):
        assert set(task.DESCRIPTIONS) == set(task.TASKS)

    def test_the_scheduler_coverage_names_tasks(self):
        assert set(task.SCHEDULER_COVERAGE.values()) <= set(task.TASKS)


class TestRun:
    def test_renders_config_limits_memory_then_runs_the_console(self):
        with patch("misp_container.task.prepare") as prepare, patch("misp_container.task.apply_defaults"), \
                patch("misp_container.task.limit_worker_memory") as limit, \
                patch("misp_container.task.subprocess.run", return_value=CompletedProcess([], 0, stdout="")) as console:
            assert task.run("update-taxonomies") == 0
        prepare.assert_called_once()
        limit.assert_called_once()
        assert console.call_args[0][0] == [CAKE, "Admin", "updateTaxonomies"]

    def test_the_consoles_exit_code_is_the_runs(self):
        assert run("pull-servers", returncode=255)[0] == 255

    def test_no_blueprint_runs_nothing_and_succeeds(self):
        assert run("sharing-group-blueprints", blueprints="0") == (0, None)

    def test_blueprints_run_the_console(self):
        assert run("sharing-group-blueprints", blueprints="2")[1][1:] == ["Admin", "executeSGBlueprint", "all"]

    def test_misp_log_lines_pass_through_once(self, capsys):
        misp = '{"time":"2026-09-30T08:06:18.647Z","level":"info","context":"misp","message":"OIDC user alice"}'
        with patch.object(task.log, "info") as info:
            run("check-user-validity", stdout=f"{misp}\nalice@example.com: valid\n")
        assert capsys.readouterr().out.splitlines() == [misp]
        assert [c.args[1] for c in info.call_args_list[1:]] == ["alice@example.com: valid"]


class TestMain:
    def test_a_failing_console_exits_1(self):
        with patch("misp_container.task.run", return_value=255):
            with pytest.raises(SystemExit) as exc:
                task.main(["check-user-validity"])
        assert exc.value.code == 1

    def test_success_exits_0_without_an_admin_key(self):
        with patch.dict(os.environ, {"ADMIN_KEY": ""}), patch("misp_container.task.run", return_value=0):
            with pytest.raises(SystemExit) as exc:
                task.main(["pull-servers"])
        assert exc.value.code == 0

    @pytest.mark.parametrize("argv", [[], ["workflow"], ["workflow", "seven"], ["update-galaxies", "7"], ["no-such"]])
    def test_a_wrong_call_exits_2(self, argv):
        with pytest.raises(SystemExit) as exc:
            task.main(argv)
        assert exc.value.code == 2
