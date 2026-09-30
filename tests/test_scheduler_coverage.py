"""Unit tests for the scheduler coverage guard (scripts/check_scheduler_coverage.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from check_scheduler_coverage import main, offered, uncovered
from misp_container import task

# The shape of MISP 2.5.47's SchedulerWorkerShell.php, reduced to the lines the
# guard reads
SHELL = """<?php
class SchedulerWorkerShell extends AppShell
{
    public const ADMIN_ACTIONS = [
        'updateGalaxies',
        'updateTaxonomies',
        'updateWarningLists',
        'updateNoticeLists',
        'updateObjectTemplates',
        // Reconcile MISP accounts with the external identity provider.
        'checkUserValidity',
        'blockInvalidUsers'
    ];

    private function processTask(array $task)
    {
        if ($task['type'] == 'Server') {
            $this->runServerTask($task);
        } elseif ($task['type'] == 'Feed') {
            if ($task['action'] === 'fetch') {
                $this->runFeedFetchTask($task);
            } elseif ($task['action'] === 'cache') {
                $this->runFeedCacheTask($task);
            }
        } elseif ($task['type'] == 'TAXII') {
            if ($task['action'] !== 'push') {
                return;
            }
        } elseif ($task['type'] == 'Workflow') {
            $this->runWorkflowAdHoc($task);
        } elseif ($task['type'] == 'Periodic Summary') {
            if ($task['action'] !== 'send') {
                return;
            }
        } elseif ($task['type'] == 'Admin') {
            $this->runAdminTask($task);
        }
    }

    private function runServerTask($task)
    {
        if (!in_array($task['action'], ['pull', 'push', 'cache'], true)) {
            return;
        }
    }

    public function runAdminTask($task)
    {
        if ($task['action'] === 'updateGalaxies') {
            $jobParams = [];
        }
        if ($task['action'] === 'updateObjectTemplates') {
            $jobParams = [];
        }
    }
}
"""


class TestOffered:
    def test_reads_types_actions_and_admin_actions(self):
        types, actions, admin = offered(SHELL)
        assert types == {"Server", "Feed", "TAXII", "Workflow", "Periodic Summary", "Admin"}
        assert actions == {"pull", "push", "cache", "fetch", "send"}
        assert "checkUserValidity" in admin and len(admin) == 7

    def test_admin_comparisons_count_as_admin_actions(self):
        _, actions, _ = offered(SHELL)
        assert "updateGalaxies" not in actions

    def test_commented_type_is_ignored(self):
        types, _, _ = offered(SHELL + "\n// if ($task['type'] == 'Retired') {\n")
        assert "Retired" not in types


class TestUncovered:
    def test_the_task_runner_covers_misp_2_5_47(self):
        assert uncovered(SHELL) == []

    def test_new_task_type(self):
        shell = SHELL.replace("$task['type'] == 'Admin'", "$task['type'] == 'Admin' || $task['type'] == 'Galaxy'")
        assert uncovered(shell) == ["task type 'Galaxy'"]

    def test_new_action(self):
        shell = SHELL.replace("['pull', 'push', 'cache']", "['pull', 'push', 'cache', 'purge']")
        assert uncovered(shell) == ["task action 'purge'"]

    def test_new_admin_action(self):
        shell = SHELL.replace("'blockInvalidUsers'\n", "'blockInvalidUsers',\n        'pruneLogs'\n")
        assert uncovered(shell) == ["admin action 'pruneLogs'"]

    def test_changed_shape_is_reported(self):
        assert "changed shape" in uncovered("<?php class Empty {}")[0]

    def test_every_covering_task_exists(self):
        assert set(task.SCHEDULER_COVERAGE.values()) <= set(task.TASKS)


class TestMain:
    def test_exit_codes(self, tmp_path, capsys):
        good = tmp_path / "good.php"
        good.write_text(SHELL)
        assert main([str(good)]) == 0
        bad = tmp_path / "bad.php"
        bad.write_text(SHELL.replace("'send'", "'mail'"))
        assert main([str(bad)]) == 1
        assert "task action 'mail'" in capsys.readouterr().out
        assert main([]) == 2
