"""Unit tests for the upstream guard (scripts/check_upstream.py).

The fixture is a MISP tree reduced to the lines the guard reads, in the shape
of the pinned MISP. Each test changes one of them the way a MISP release could.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import check_upstream as cu  # noqa: E402
from misp_container import task  # noqa: E402

# The shape of MISP's SchedulerWorkerShell.php, reduced to the lines the
# guard reads
SCHEDULER_SHELL = """<?php
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


BACKGROUND_JOBS = """<?php
class BackgroundJobsTool
{
    const MISP_WORKERS_PROCESS_GROUP = 'misp-workers';

    const
        DEFAULT_QUEUE = 'default',
        EMAIL_QUEUE = 'email',
        CACHE_QUEUE = 'cache',
        PRIO_QUEUE = 'prio',
        UPDATE_QUEUE = 'update',
        SCHEDULER_QUEUE = 'scheduler';

    const VALID_QUEUES = [
        self::DEFAULT_QUEUE,
        self::EMAIL_QUEUE,
        self::CACHE_QUEUE,
        self::PRIO_QUEUE,
        self::UPDATE_QUEUE,
        self::SCHEDULER_QUEUE,
    ];

    const JOB_STATUS_PREFIX = 'job_status',
        DATA_CONTENT_PREFIX = 'data_content',
        RUNNING_JOB_PREFIX = 'running';

    public function enqueue($queue, $command, $args = [])
    {
        $this->RedisConnection->rpush($queue, $backgroundJob);
    }

    public function markAsRunning(Worker $worker, BackgroundJob $job, $pid = null)
    {
        $key = self::RUNNING_JOB_PREFIX . ':' . $worker->queue() . ':' . $job->id();
        $this->RedisConnection->setex($key, 60, []);
    }

    private function createRedisConnection()
    {
        $redis->setOption(Redis::OPT_PREFIX, $this->settings['redis_namespace'] . ':');
    }
}
"""

# The console shells, with every command the tasks run
SHELLS = {}
for _shell, _command, *_ in cu.TASKS.values():
    SHELLS.setdefault(_shell, []).append(f"    public function {_command}()\n    {{\n    }}\n")

CONSOLE_SHELL = """<?php
class Shell extends CakeObject {
	protected function _useLogger($enable = true) {
		if (!$enable) {
			CakeLog::drop('stdout');
			CakeLog::drop('stderr');
			return;
		}
		if (!$this->_loggerIsConfigured("stdout")) {
			$this->_configureStdOutLogger();
		}
		if (!$this->_loggerIsConfigured("stderr")) {
			$this->_configureStdErrLogger();
		}
	}
}
"""

BOOTSTRAP = """<?php
if (Configure::read('OidcAuth')) {
	CakePlugin::load('OidcAuth');
}

if (empty(Configure::read('SimpleBackgroundJobs.enabled'))) {
	CakePlugin::loadAll(array(
		'CakeResque' => array('bootstrap' => true)
	));
}

/**
 * Configures default file logging options
 */
App::uses('CakeLog', 'Log');
CakeLog::config('debug', array(
	'engine' => 'FileLog',
	'types' => array('notice', 'info', 'debug'),
	'file' => 'debug',
));
CakeLog::config('error', array(
	'engine' => 'FileLog',
	'types' => array('warning', 'error', 'critical', 'alert', 'emergency'),
	'file' => 'error',
));
"""

ATTACHMENT_TOOL = """<?php
class AttachmentTool
{
    public function attachmentDirIsS3()
    {
        $attachmentsDir = Configure::read('MISP.attachments_dir');
        return $attachmentsDir && str_starts_with($attachmentsDir, "s3");
    }

    private function getPath($shadow, $eventId, $attributeId, $pathSuffix, $forceNonBucketed = false)
    {
        $path = $shadow ? ('shadow' . DS) : '';
        if (Configure::read('MISP.attachments_bucketed') && empty($forceNonBucketed) && !$this->attachmentDirIsS3()) {
            return $path . 'bucket_' . (1000*(floor($eventId / 1000))) . DS . $eventId . DS . $attributeId . $pathSuffix;
        }
        return $path . $eventId . DS . $attributeId . $pathSuffix;
    }
}
"""

AWS_S3_CLIENT = """<?php
class AWSS3Client
{
    public function initTool()
    {
        if ($settings['aws_compatible']) {
            $s3Config = array(
                 'version' => 'latest',
                 'region' => $settings['region'],
                 'endpoint' => $settings['aws_endpoint'],
                 'use_path_style_endpoint' => true,
            );
        }
    }
}
"""

# The files MISP writes under app/tmp/logs directly, one line each, as MISP writes them
LOG_WRITERS = {
    "app/Lib/Tools/ServerSyncTool.php":
        "<?php file_put_contents(APP . 'tmp/logs/server-sync.log', $logEntry, FILE_APPEND | LOCK_EX);",
    "app/Model/Workflow.php": "<?php\n// file_put_contents(APP . 'tmp/logs/old-workflow.log', $logEntry);\n"
                              "FileAccessTool::writeToFile(APP . 'tmp/logs/workflow-execution.log', $logEntry, false, true);",
    "app/Lib/Tools/ProcessTool.php": "<?php const LOG_FILE = APP . 'tmp/logs/exec-errors.log';",
    "app/Lib/Tools/KafkaPubTool.php":
        "<?php error_log($msg, 3, APP . 'tmp' . DS . 'logs' . DS . 'kafka.error.log');",
    "app/Lib/Tools/JsonLogTool.php": "<?php public $logFilePath = APP . 'tmp/logs/error.log.ndjson';",
    "app/Model/Organisation.php": "<?php $dir = APP . 'tmp/logs/merges/';",
    "app/Controller/AttributesController.php":
        "<?php file_put_contents(APP . '/tmp/logs/missing_attachments.log', json_encode($results));",
    "app/Error/AppExceptionRenderer.php":
        "<?php file_put_contents(LOGS . 'fatal_error.log', $logMessage . PHP_EOL, FILE_APPEND);",
    "app/Lib/Tools/PubSubTool.php":
        "<?php shell_exec(ProcessTool::pythonBin() . ' ' . APP . 'files' . DS . 'scripts' . DS . 'mispzmq' . DS . "
        "'mispzmq.py >> ' . APP . 'tmp' . DS . 'logs' . DS . 'mispzmq.log 2>> ' . APP . 'tmp' . DS . 'logs' . DS . "
        "'mispzmq.error.log &');",
}


SERVER_MODEL = """<?php
class Server extends AppModel
{
    public function getFileRules()
    {
        return [
            'orgs' => [
                'name' => __('Organisation logos'),
                'path' => APP . 'files' . DS . 'img' . DS . 'orgs',
                'regex' => '.*\\.(png|svg)$',
                'files' => [],
            ],
            'img' => [
                'name' => __('Additional image files'),
                'expected' => [
                    'MISP.footer_logo' => Configure::read('MISP.footer_logo'),
                ],
                'path' => APP . 'files' . DS . 'img' . DS . 'custom',
                'regex' => '.*\\.(png|svg)$',
                'files' => array(),
            ],
        ];
    }
}
"""

ENV_SETTING = """<?php
class EnvSetting
{
    const ENV_PREFIX = 'MISP_SETTING_';

    public static function readSettings()
    {
        $settingName = str_replace('__', '.', substr($name, strlen(self::ENV_PREFIX)));
    }
}
"""

SYSTEM_SETTING = """<?php
class SystemSetting extends AppModel
{
    const ALLOWED_CATEGORIES = [
        'SimpleBackgroundJobs', 'MISP', 'Security', 'GnuPG', 'SMIME', 'Proxy', 'SecureAuth',
        'Session', 'Plugin', 'debug', 'site_admin_debug',
    ];
}
"""

ORG_IMG_HELPER = """<?php
class OrgImgHelper extends AppHelper
{
    const IMG_PATH = APP . 'files' . DS . 'img' . DS . 'orgs' . DS;
}
"""


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def misp(tmp_path):
    """A MISP tree that passes every check."""
    root = tmp_path / "MISP"
    write(root, cu.BACKGROUND_JOBS, BACKGROUND_JOBS)
    write(root, cu.SCHEDULER_SHELL, SCHEDULER_SHELL)
    for shell, methods in SHELLS.items():
        write(root, f"{cu.CONSOLE_COMMANDS}/{shell}Shell.php", "<?php\nclass X {\n" + "".join(methods) + "}\n")
    write(root, cu.CONSOLE_SHELL, CONSOLE_SHELL)
    write(root, cu.BOOTSTRAP, BOOTSTRAP)
    write(root, cu.ATTACHMENT_TOOL, ATTACHMENT_TOOL)
    write(root, cu.AWS_S3_CLIENT, AWS_S3_CLIENT)
    write(root, cu.SERVER_MODEL, SERVER_MODEL)
    write(root, cu.ORG_IMG_HELPER, ORG_IMG_HELPER)
    write(root, cu.ENV_SETTING, ENV_SETTING)
    write(root, cu.SYSTEM_SETTING, SYSTEM_SETTING)
    for rel, text in LOG_WRITERS.items():
        write(root, rel, text)
    actions: dict[str, list[str]] = {}
    for route in cu.called_routes():
        controller, action = cu.controller_action(route)
        actions.setdefault(controller, []).append(action)
    for controller, names in actions.items():
        # Appended: AttributesController also carries a log writer
        path = root / cu.CONTROLLERS / controller
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write("\n".join(f"public function {name}($id = null) {{}}" for name in names))
    return root


def edit(root, rel, old, new):
    path = root / rel
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def problems(misp, name):
    return cu.run(misp)[name]


class TestTheFixture:
    def test_every_check_passes(self, misp):
        assert cu.run(misp) == {check.name: [] for check in cu.CHECKS}

    def test_a_problem_names_the_upstream_file_and_ours(self, misp):
        edit(misp, cu.BACKGROUND_JOBS, "SCHEDULER_QUEUE = 'scheduler';", "SCHEDULER_QUEUE = 'scheduler',\n KAFKA_QUEUE = 'kafka';")
        edit(misp, cu.BACKGROUND_JOBS, "self::SCHEDULER_QUEUE,\n", "self::SCHEDULER_QUEUE,\n self::KAFKA_QUEUE,\n")
        [problem] = problems(misp, "worker-queues")
        assert f"upstream: {cu.BACKGROUND_JOBS}" in problem and "ours: files/misp_container/__init__.py" in problem


class TestConsoleCommands:
    def test_a_renamed_command_fails_its_task(self, misp):
        edit(misp, f"{cu.CONSOLE_COMMANDS}/ServerShell.php", "function pull()", "function pullEverything()")
        [problem] = problems(misp, "console-commands")
        assert "pull-servers" in problem and "pull()" in problem

    def test_a_missing_shell_fails_its_tasks(self, misp):
        (misp / cu.CONSOLE_COMMANDS / "WorkflowShell.php").unlink()
        [problem] = problems(misp, "console-commands")
        assert problem.startswith(f"workflow: MISP has no {cu.CONSOLE_COMMANDS}/WorkflowShell.php")


class TestWorkerQueues:
    def test_a_new_queue_needs_workers(self, misp):
        edit(misp, cu.BACKGROUND_JOBS, "SCHEDULER_QUEUE = 'scheduler';", "SCHEDULER_QUEUE = 'scheduler',\n KAFKA_QUEUE = 'kafka';")
        edit(misp, cu.BACKGROUND_JOBS, "self::SCHEDULER_QUEUE,\n", "self::SCHEDULER_QUEUE,\n self::KAFKA_QUEUE,\n")
        [problem] = problems(misp, "worker-queues")
        assert "'kafka'" in problem and "NUM_WORKERS_KAFKA" in problem

    def test_a_dropped_queue(self, misp):
        edit(misp, cu.BACKGROUND_JOBS, "        self::EMAIL_QUEUE,\n", "")
        assert "'email', which MISP no longer offers" in problems(misp, "worker-queues")[0]

    def test_a_new_shape(self, misp):
        edit(misp, cu.BACKGROUND_JOBS, "const VALID_QUEUES", "const QUEUES")
        assert "changed shape" in problems(misp, "worker-queues")[0]


class TestJobKeys:
    def test_a_waiting_job_elsewhere(self, misp):
        edit(misp, cu.BACKGROUND_JOBS, "->rpush($queue,", "->rpush('queue:' . $queue,")
        assert "a waiting job" in problems(misp, "job-keys")[0]

    def test_a_new_running_prefix(self, misp):
        edit(misp, cu.BACKGROUND_JOBS, "RUNNING_JOB_PREFIX = 'running'", "RUNNING_JOB_PREFIX = 'active'")
        assert "'active'" in problems(misp, "job-keys")[0]

    def test_a_running_key_without_the_queue(self, misp):
        edit(misp, cu.BACKGROUND_JOBS, " . $worker->queue() . ':'", "")
        assert "a running job" in problems(misp, "job-keys")[0]


class TestSchedulerTasks:
    def test_a_new_task_type(self, misp):
        edit(misp, cu.SCHEDULER_SHELL, "} elseif ($task['type'] == 'Feed') {",
             "} elseif ($task['type'] == 'Report') {\n        } elseif ($task['type'] == 'Feed') {")
        assert any("'Report'" in p for p in problems(misp, "scheduler-tasks"))


class TestApiRoutes:
    def test_mapping(self):
        assert cu.controller_action("sharing_groups/addOrg") == ("SharingGroupsController.php", "addOrg")
        assert cu.controller_action("admin/users") == ("UsersController.php", "admin_index")
        assert cu.controller_action("admin/users/edit") == ("UsersController.php", "admin_edit")
        assert cu.controller_action("taxiiServers/push") == ("TaxiiServersController.php", "push")
        assert cu.controller_action("organisations") == ("OrganisationsController.php", "index")

    def test_the_callers_name_the_routes(self):
        routes = cu.called_routes()
        assert {"servers/add", "admin/users/edit", "users/login", "servers/getVersion", "roles"} <= set(routes)
        assert "misp_container/sync.py" in routes["servers/add"][0]

    def test_a_removed_action(self, misp):
        edit(misp, f"{cu.CONTROLLERS}/ServersController.php", "public function getVersion(", "public function version(")
        [problem] = problems(misp, "api-routes")
        assert problem.startswith("/servers/getVersion (misp_container/metrics.py:") and "has no action getVersion" in problem

    def test_a_removed_controller(self, misp):
        (misp / cu.CONTROLLERS / "WarninglistsController.php").unlink()
        found = problems(misp, "api-routes")
        assert found and all("MISP has no WarninglistsController.php" in p for p in found)


class TestAttachmentKeys:
    def test_buckets_on_s3(self, misp):
        edit(misp, cu.ATTACHMENT_TOOL, " && !$this->attachmentDirIsS3()", "")
        assert "bucket_<n> on disk only" in problems(misp, "attachment-keys")[0]

    def test_a_new_key_shape(self, misp):
        edit(misp, cu.ATTACHMENT_TOOL, "return $path . $eventId . DS . $attributeId . $pathSuffix;",
             "return $path . $eventId . '-' . $attributeId . $pathSuffix;")
        assert "<event>/<attribute><suffix>" in problems(misp, "attachment-keys")[0]

    def test_virtual_hosted_requests(self, misp):
        edit(misp, cu.AWS_S3_CLIENT, "'use_path_style_endpoint' => true", "'use_path_style_endpoint' => false")
        assert "path-style" in problems(misp, "attachment-keys")[0]


class TestBootstrap:
    def test_a_new_file_stream(self, misp):
        write(misp, cu.BOOTSTRAP, BOOTSTRAP + "CakeLog::config('audit', array('engine' => 'FileLog', 'file' => 'audit'));\n")
        [problem] = problems(misp, "cakelog-streams")
        assert "'audit'" in problem and "app/tmp/logs/audit.log" in problem

    def test_a_renamed_stream(self, misp):
        edit(misp, cu.BOOTSTRAP, "CakeLog::config('debug',", "CakeLog::config('info',")
        found = problems(misp, "cakelog-streams")
        assert len(found) == 2 and "'info'" in found[0] and "'debug'" in found[1]

    def test_shell_streams_renamed(self, misp):
        edit(misp, cu.CONSOLE_SHELL, '_loggerIsConfigured("stdout")', '_loggerIsConfigured("out")')
        assert "appear twice" in problems(misp, "shell-streams")[0]


class TestRelayedFiles:
    def test_a_new_log_file(self, misp):
        write(misp, "app/Model/Report.php", "<?php file_put_contents(APP . 'tmp/logs/report.log', $x);")
        [problem] = problems(misp, "relayed-files")
        assert "app/tmp/logs/report.log directly (app/Model/Report.php)" in problem

    def test_a_file_misp_stopped_writing(self, misp):
        (misp / "app/Lib/Tools/ServerSyncTool.php").unlink()
        assert "forwards server-sync.log, which MISP no longer writes" in problems(misp, "relayed-files")[0]

    def test_a_commented_write_is_no_write(self, misp):
        assert not any("old-workflow.log" in p for p in problems(misp, "relayed-files"))

    def test_json_log_tool_called(self, misp):
        write(misp, "app/Model/Server.php", "<?php $tool = new JsonLogTool();")
        assert "calls JsonLogTool now (app/Model/Server.php)" in problems(misp, "relayed-files")[0]

    def test_a_stream_the_logging_block_drops_is_no_file_misp_writes(self, misp, monkeypatch):
        monkeypatch.setattr(cu, "RELAYED", cu.RELAYED + ("debug.log",))
        assert "forwards debug.log, which MISP no longer writes" in problems(misp, "relayed-files")[0]

    def test_framework_files_are_not_misp(self, misp):
        write(misp, "app/Lib/cakephp/lib/Cake/Log/Engine/FileLog.php", "<?php $f = 'tmp/logs/cake.log';")
        assert problems(misp, "relayed-files") == []


class TestLogBlockPhp:
    def test_without_php(self, misp, monkeypatch):
        monkeypatch.setattr(cu.shutil, "which", lambda name: None)
        assert cu.log_block_php(misp) == ["php is not on PATH"]


class TestMain:
    def test_exit_0_and_ok_lines(self, misp, capsys):
        assert cu.main([str(misp)]) == 0
        assert capsys.readouterr().out.startswith("ok   worker-queues")

    def test_exit_1_and_json(self, misp, capsys):
        (misp / cu.CONSOLE_SHELL).write_text("<?php")
        assert cu.main([str(misp), "--json"]) == 1
        results = json.loads(capsys.readouterr().out)
        assert results["shell-streams"] and not results["worker-queues"]

    def test_a_missing_file_is_a_problem(self, misp):
        (misp / cu.BACKGROUND_JOBS).unlink()
        assert problems(misp, "worker-queues")[0].startswith(f"cannot read {cu.BACKGROUND_JOBS}")


class TestImagePaths:
    def test_logos_moved(self, misp):
        edit(misp, cu.SERVER_MODEL, "'files' . DS . 'img' . DS . 'orgs'", "'webroot' . DS . 'img' . DS . 'orgs'")
        assert any("uploads 'orgs' files to app/webroot/img/orgs" in p for p in problems(misp, "image-paths"))

    def test_custom_images_moved(self, misp):
        edit(misp, cu.SERVER_MODEL, "'files' . DS . 'img' . DS . 'custom'", "'files' . DS . 'custom'")
        assert any("uploads 'img' files to app/files/custom" in p for p in problems(misp, "image-paths"))

    def test_helper_reads_elsewhere(self, misp):
        edit(misp, cu.ORG_IMG_HELPER, "APP . 'files' . DS . 'img' . DS . 'orgs' . DS", "APP . 'webroot' . DS . 'img' . DS . 'orgs' . DS")
        assert any("reads org logos from app/webroot/img/orgs" in p for p in problems(misp, "image-paths"))

    def test_rules_gone(self, misp):
        edit(misp, cu.SERVER_MODEL, "function getFileRules(", "function getUploadRules(")
        assert any("no getFileRules()" in p for p in problems(misp, "image-paths"))


class TestOffered:
    def test_reads_types_actions_and_admin_actions(self):
        types, actions, admin = cu.offered(SCHEDULER_SHELL)
        assert types == {"Server", "Feed", "TAXII", "Workflow", "Periodic Summary", "Admin"}
        assert actions == {"pull", "push", "cache", "fetch", "send"}
        assert "checkUserValidity" in admin and len(admin) == 7

    def test_admin_comparisons_count_as_admin_actions(self):
        _, actions, _ = cu.offered(SCHEDULER_SHELL)
        assert "updateGalaxies" not in actions

    def test_commented_type_is_ignored(self):
        types, _, _ = cu.offered(SCHEDULER_SHELL + "\n// if ($task['type'] == 'Retired') {\n")
        assert "Retired" not in types


class TestUncovered:
    def test_the_task_runner_covers_misp_2_5_47(self):
        assert cu.uncovered(SCHEDULER_SHELL) == []

    def test_new_task_type(self):
        shell = SCHEDULER_SHELL.replace("$task['type'] == 'Admin'", "$task['type'] == 'Admin' || $task['type'] == 'Galaxy'")
        assert cu.uncovered(shell) == ["task type 'Galaxy'"]

    def test_new_action(self):
        shell = SCHEDULER_SHELL.replace("['pull', 'push', 'cache']", "['pull', 'push', 'cache', 'purge']")
        assert cu.uncovered(shell) == ["task action 'purge'"]

    def test_new_admin_action(self):
        shell = SCHEDULER_SHELL.replace("'blockInvalidUsers'\n", "'blockInvalidUsers',\n        'pruneLogs'\n")
        assert cu.uncovered(shell) == ["admin action 'pruneLogs'"]

    def test_changed_shape_is_reported(self):
        assert "changed shape" in cu.uncovered("<?php class Empty {}")[0]

    def test_every_covering_task_exists(self):
        assert set(task.SCHEDULER_COVERAGE.values()) <= set(task.TASKS)


class TestEnvSettings:
    def test_a_new_prefix(self, misp):
        edit(misp, cu.ENV_SETTING, "'MISP_SETTING_'", "'MISP_CONFIG_'")
        assert "MISP_SETTING_*" in problems(misp, "env-settings")[0]

    def test_a_category_dropped(self, misp):
        edit(misp, cu.SYSTEM_SETTING, "'Proxy', ", "")
        [problem] = problems(misp, "env-settings")
        assert "no longer holds Proxy" in problem


class TestDroppedFiles:
    def test_misp_code_that_names_the_asn_database_is_reported(self, misp):
        write(misp, "app/Lib/Tools/GeoTool.php", "<?php\n$db = APP . 'files/geo-open/GeoOpen-Country-ASN.mmdb';\n")
        [problem] = problems(misp, "dropped-files")
        assert "app/Lib/Tools/GeoTool.php names GeoOpen-Country-ASN" in problem

    def test_the_release_step_and_comments_may_name_it(self, misp):
        write(misp, "app/Console/Command/AdminShell.php", "<?php\n$help = __('from the GeoOpen-Country-ASN mmdb');\n")
        write(misp, "app/Lib/Dashboard/Widget.php", "<?php\n// built from GeoOpen-Country-ASN.mmdb\n")
        assert problems(misp, "dropped-files") == []

    def test_a_view_that_loads_misp_iconify_sources_is_reported(self, misp):
        write(misp, "app/View/Layouts/x.ctp", "<link href=\"/files/misp-iconify/exports/icons.css\">\n")
        assert problems(misp, "dropped-files")
