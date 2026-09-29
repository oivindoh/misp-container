#!/usr/bin/env python3
"""Fail when MISP no longer matches what this image patches or depends on.

Usage: check_upstream.py <MISP root> [--php] [--json]

<MISP root> is /var/www/MISP in the image, or a MISP checkout. Each check
reads MISP's own files and names the file of ours that depends on them. A
failure says what changed upstream and where to revisit. --php also runs the
logging block that init.py renders into bootstrap.php through PHP, in both
formats. --json prints {check: [problems]}. The integration suite runs all
checks in the image (tests/e2e/test_upstream.py).
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SCRIPTS = Path(__file__).resolve().parent
FILES = SCRIPTS.parent / "files"
sys.path.insert(0, str(FILES))
sys.path.insert(0, str(SCRIPTS))

from check_scheduler_coverage import uncovered  # noqa: E402
from misp_container import CONFIG_DEFAULTS, WORKER_GROUP, WORKER_QUEUES  # noqa: E402
from misp_container.init import LOG_BLOCK, render_log_block  # noqa: E402
from misp_container.logrelay import FILES as RELAYED  # noqa: E402
from misp_container.metrics import RUNNING_PREFIX  # noqa: E402

BACKGROUND_JOBS = "app/Lib/Tools/BackgroundJobsTool.php"
SCHEDULER_SHELL = "app/Console/Command/SchedulerWorkerShell.php"
CONSOLE_SHELL = "app/Lib/cakephp/lib/Cake/Console/Shell.php"
CONTROLLERS = "app/Controller"
BOOTSTRAP = "app/Config/bootstrap.default.php"
# The directories of MISP's own PHP code, and the parts in them that are not MISP's
MISP_PHP = ("app/Model", "app/Lib", "app/Controller", "app/Console", "app/Plugin")
NOT_MISP_PARTS = {"cakephp", "Vendor"}

# The modules that call MISP's HTTP API; every route-like string in them is a MISP route
API_CALLERS = ("misp_container/api.py", "misp_container/sync.py", "misp_container/task.py",
               "misp_container/metrics.py", "entrypoint-sync.py")
ROUTE = re.compile(r"^/([a-z][A-Za-z_]*(?:/[a-z][A-Za-z_]*){0,2})(?:[/?.{]|$)")

# Files MISP writes under app/tmp/logs that the relay leaves alone, with the reason
NOT_RELAYED = {
    "error.log.ndjson": "JsonLogTool writes it, and no MISP code outside JsonLogTool.php calls JsonLogTool",
    "missing_attachments.log": "a one-off report of an admin action (AttributesController), not a log",
    "merges": "a directory of the backups an organisation merge writes (Organisation.php), not a log",
}


@dataclass
class Check:
    name: str
    upstream: str
    ours: str
    run: Callable[[Path], list[str]]


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text(errors="replace")


def misp_php(root: Path):
    """MISP's own PHP files, framework and vendored code left out."""
    for top in MISP_PHP:
        if (root / top).is_dir():
            for path in sorted((root / top).rglob("*.php")):
                if not NOT_MISP_PARTS.intersection(path.relative_to(root).parts):
                    yield path


def strip_php_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*(//|#).*$", "", source)


def bootstrap_default(root: Path) -> Path:
    """The upstream bootstrap.php template: in a checkout under app/Config, in the image under CONFIG_DEFAULTS."""
    checkout = root / BOOTSTRAP
    return checkout if checkout.exists() else Path(CONFIG_DEFAULTS) / "bootstrap.default.php"


# -- worker queues and the Redis layout ----------------------------------------------

def worker_queues(root: Path) -> list[str]:
    source = strip_php_comments(read(root, BACKGROUND_JOBS))
    constants = dict(re.findall(r"\b(\w+_QUEUE)\s*=\s*'(\w+)'", source))
    listed = re.search(r"const\s+VALID_QUEUES\s*=\s*\[(.*?)\];", source, re.S)
    if not listed:
        return ["no VALID_QUEUES constant: the file changed shape; update worker_queues() in this script"]
    valid = {constants.get(name, name) for name in re.findall(r"self::(\w+)", listed.group(1))}
    upstream = valid - {constants.get("SCHEDULER_QUEUE")}
    problems = [f"MISP has queue {q!r}, which no worker runs: its jobs wait for ever. Add it to WORKER_QUEUES "
                f"and NUM_WORKERS_{q.upper()} to deploy/base/base.env" for q in sorted(upstream - set(WORKER_QUEUES))]
    problems += [f"WORKER_QUEUES has {q!r}, which MISP no longer offers" for q in sorted(set(WORKER_QUEUES) - upstream)]
    return problems


def worker_group(root: Path) -> list[str]:
    found = re.search(r"MISP_WORKERS_PROCESS_GROUP\s*=\s*'([^']+)'", read(root, BACKGROUND_JOBS))
    if not found:
        return ["no MISP_WORKERS_PROCESS_GROUP constant: MISP finds its workers another way now"]
    if found.group(1) != WORKER_GROUP:
        return [f"MISP finds its workers in the supervisord group {found.group(1)!r}; the worker entrypoint "
                f"writes {WORKER_GROUP!r}, so the diagnostic page and worker restarts see no workers"]
    return []


def job_keys(root: Path) -> list[str]:
    source = strip_php_comments(read(root, BACKGROUND_JOBS))
    expected = {
        "the key prefix <namespace>:": r"OPT_PREFIX\s*,\s*\$this->settings\['redis_namespace'\]\s*\.\s*':'",
        "a waiting job in the list <queue>": r"->rpush\(\s*\$queue\s*,",
        "a running job at running:<queue>:<job id>":
            r"self::RUNNING_JOB_PREFIX\s*\.\s*':'\s*\.\s*\$worker->queue\(\)\s*\.\s*':'\s*\.\s*\$job->id\(\)",
    }
    problems = [f"MISP no longer stores {what}. misp_jobs_queued reads 0 and the task runner's backlog "
                f"check never holds a run" for what, pattern in expected.items() if not re.search(pattern, source)]
    prefix = re.search(r"RUNNING_JOB_PREFIX\s*=\s*'([^']+)'", source)
    if prefix and prefix.group(1) != RUNNING_PREFIX:
        problems.append(f"MISP's running-job prefix is {prefix.group(1)!r}; RUNNING_PREFIX is {RUNNING_PREFIX!r}")
    return problems


def scheduler_tasks(root: Path) -> list[str]:
    return [f"not covered by the task runner: {item}" for item in uncovered(read(root, SCHEDULER_SHELL))]


# -- API routes ----------------------------------------------------------------------

def called_routes(files: Path = FILES) -> dict[str, list[str]]:
    """Every MISP route the API callers name, with where: {"servers/pull": ["misp_container/task.py:35"]}."""
    routes: dict[str, list[str]] = {}
    for rel in API_CALLERS:
        for node in ast.walk(ast.parse((files / rel).read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found = ROUTE.match(node.value)
                if found:
                    routes.setdefault(found.group(1), []).append(f"{rel}:{node.lineno}")
    return routes


def controller_action(route: str) -> tuple[str, str]:
    """("ServersController.php", "pull") for "servers/pull"; admin routes map to admin_<action>."""
    parts = route.split("/")
    prefix = ""
    if parts[0] == "admin":
        prefix, parts = "admin_", parts[1:]
    controller = "".join(p[:1].upper() + p[1:] for p in parts[0].split("_")) + "Controller.php"
    return controller, prefix + (parts[1] if len(parts) > 1 else "index")


def api_routes(root: Path, routes: dict[str, list[str]] | None = None) -> list[str]:
    problems = []
    for route, where in sorted((routes or called_routes()).items()):
        controller, action = controller_action(route)
        path = root / CONTROLLERS / controller
        if not path.exists():
            problems.append(f"/{route} ({', '.join(where)}): MISP has no {controller}")
        elif not re.search(rf"public\s+function\s+{action}\s*\(", path.read_text(errors="replace")):
            problems.append(f"/{route} ({', '.join(where)}): {controller} has no action {action}")
    return problems


# -- bootstrap.php and the logging block ---------------------------------------------

def _blocks(source: str, opening: str) -> list[tuple[int, int]]:
    """The spans of the brace blocks that follow each occurrence of opening."""
    spans = []
    for found in re.finditer(opening, source):
        start = source.find("{", found.end())
        depth, i = 0, start
        while i < len(source):
            depth += {"{": 1, "}": -1}.get(source[i], 0)
            if depth == 0:
                spans.append((start, i))
                break
            i += 1
    return spans


def cakeresque(root: Path) -> list[str]:
    source = strip_php_comments(bootstrap_default(root).read_text())
    guarded = _blocks(source, r"if\s*\(\s*empty\(\s*Configure::read\(\s*'SimpleBackgroundJobs\.enabled'\s*\)\s*\)\s*\)")
    loose = [m.start() for m in re.finditer(r"CakeResque", source)
             if not any(a < m.start() < b for a, b in guarded)]
    if loose:
        return ["bootstrap.default.php loads CakeResque outside if (empty(SimpleBackgroundJobs.enabled)). The image "
                "drops iglocska/cake-resque from composer.json (Dockerfile, stage composer-prep), so MISP stops "
                "at start with MissingPluginException. Remove the load in init.py prepare_config()"]
    return []


def file_streams(source: str) -> dict[str, str]:
    """{stream name: log file} of the CakeLog FileLog streams bootstrap.php configures."""
    streams = {}
    for name, body in re.findall(r"CakeLog::config\(\s*'(\w+)'\s*,\s*array\((.*?)\)\s*\);", source, re.S):
        if re.search(r"'engine'\s*=>\s*'File(Log)?'", body):
            file = re.search(r"'file'\s*=>\s*'([\w.-]+)'", body)
            streams[name] = f"{file.group(1) if file else name}.log"
    return streams


def cakelog_streams(root: Path) -> list[str]:
    streams = file_streams(strip_php_comments(bootstrap_default(root).read_text()))
    dropped = set(re.findall(r"CakeLog::drop\('(\w+)'\)", LOG_BLOCK))
    problems = [f"bootstrap.default.php configures the FileLog stream {name!r}, which the logging block does not "
                f"drop: MISP writes app/tmp/logs/{file} again" for name, file in sorted(streams.items())
                if name not in dropped]
    problems += [f"the logging block drops the stream {name!r}, which bootstrap.default.php no longer configures"
                 for name in sorted(dropped - set(streams))]
    return problems


def shell_streams(root: Path) -> list[str]:
    checked = set(re.findall(r"_loggerIsConfigured\(\s*['\"](\w+)['\"]\s*\)", read(root, CONSOLE_SHELL)))
    reserved = set(re.findall(r"CakeLog::config\('(\w+)', array\('engine' => 'ContainerLog', "
                              r"'types' => array\('none'\)\)\);", LOG_BLOCK))
    if checked != reserved:
        return [f"Shell::_useLogger() adds its console streams unless streams named {sorted(checked)} exist; the "
                f"logging block reserves {sorted(reserved)}. Every console log line would appear twice"]
    return []


def relayed_files(root: Path) -> list[str]:
    written: dict[str, list[str]] = {}
    callers = []
    for path in misp_php(root):
        source = strip_php_comments(path.read_text(errors="replace"))
        if path.name != "JsonLogTool.php" and "JsonLogTool" in source:
            callers.append(str(path.relative_to(root)))
        names = re.findall(r"tmp/logs/([\w.-]+)", source)
        names += re.findall(r"'logs'\s*\.\s*DS\s*\.\s*'([\w.-]+)'", source)
        for name in names:
            written.setdefault(name.rstrip("."), []).append(str(path.relative_to(root)))
    for file in file_streams(strip_php_comments(bootstrap_default(root).read_text())).values():
        written.setdefault(file, []).append("app/Config/bootstrap.default.php")
    problems = [f"MISP writes app/tmp/logs/{name} directly ({', '.join(sorted(set(where)))}); the relay does not "
                f"forward it, and the file grows until the volume fills" for name, where in sorted(written.items())
                if name not in RELAYED and name not in NOT_RELAYED]
    problems += [f"the relay forwards {name}, which MISP no longer writes" for name in RELAYED if name not in written]
    if callers:
        problems.append(f"MISP calls JsonLogTool now ({', '.join(callers)}): error.log.ndjson needs the relay")
    return problems


# -- the rendered logging block in PHP -----------------------------------------------

# Stand-ins for the CakePHP classes the block touches, so PHP runs it without MISP
PHP_STUBS = """
class App { public static function uses($class, $package) {} }
class BaseLog { public function __construct($config = array()) {} }
class CakeLog { public static function drop($name) {} public static function config($name, $config) {} }
"""
PROBE = 'probe "quoted" \\ line'


def log_block_php(root: Path, php: str | None = None) -> list[str]:
    php = php or shutil.which("php")
    if not php:
        return ["php is not on PATH"]
    problems = []
    for fmt in ("json", "text"):
        block = render_log_block("<?php\n", fmt).removeprefix("<?php\n")
        code = "<?php\n" + PHP_STUBS + block + f"\n(new ContainerLog())->write('error', {json.dumps(PROBE)});\n"
        lint = subprocess.run([php, "-l"], input=code, capture_output=True, text=True)
        if lint.returncode != 0:
            problems.append(f"the {fmt} block is no valid PHP: {(lint.stdout + lint.stderr).strip()}")
            continue
        ran = subprocess.run([php], input=code, capture_output=True, text=True)
        line = ran.stderr.strip()
        if fmt == "json":
            try:
                entry = json.loads(line)
                ok = (entry.get("level"), entry.get("context"), entry.get("message")) == ("error", "misp", PROBE)
            except ValueError:
                ok = False
        else:
            ok = re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ERROR \[misp\] " + re.escape(PROBE), line) is not None
        if ran.returncode != 0 or not ok:
            problems.append(f"the {fmt} writer wrote {line!r} (exit {ran.returncode}): {ran.stdout.strip()}")
    return problems


CHECKS = [
    Check("worker-queues", BACKGROUND_JOBS, "files/misp_container/__init__.py WORKER_QUEUES", worker_queues),
    Check("worker-group", BACKGROUND_JOBS, "files/misp_container/__init__.py WORKER_GROUP", worker_group),
    Check("job-keys", BACKGROUND_JOBS, "files/misp_container/metrics.py _collect_queue_metrics()", job_keys),
    Check("scheduler-tasks", SCHEDULER_SHELL, "files/misp_container/task.py SCHEDULER_COVERAGE", scheduler_tasks),
    Check("api-routes", CONTROLLERS, "the file and line named in each problem", api_routes),
    Check("cakeresque", BOOTSTRAP, "Dockerfile stage composer-prep", cakeresque),
    Check("cakelog-streams", BOOTSTRAP, "files/misp_container/init.py LOG_BLOCK", cakelog_streams),
    Check("shell-streams", CONSOLE_SHELL, "files/misp_container/init.py LOG_BLOCK", shell_streams),
    Check("relayed-files", "app/Model, app/Lib, app/Controller, app/Console, app/Plugin",
          "files/misp_container/logrelay.py FILES", relayed_files),
]
PHP_CHECK = Check("log-block-php", "php", "files/misp_container/init.py LOG_BLOCK", log_block_php)


def run(root: Path, php: bool = False) -> dict[str, list[str]]:
    """{check name: problems}; a check that cannot read its file reports that as its problem."""
    results = {}
    for check in CHECKS + ([PHP_CHECK] if php else []):
        try:
            problems = check.run(root)
        except OSError as e:
            problems = [f"cannot read {check.upstream}: {e}"]
        results[check.name] = [f"{p}\n    upstream: {check.upstream}\n    ours: {check.ours}" for p in problems]
    return results


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    results = run(Path(args[0]), php="--php" in argv)
    if "--json" in argv:
        print(json.dumps(results))
    else:
        for name, problems in results.items():
            print(f"{'FAIL' if problems else 'ok  '} {name}")
            for problem in problems:
                print(f"  {problem}")
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
