"""Options and shared fixtures for the stack suites (tests/e2e).

The suites start real Compose stacks; run them one module at a time:
    pytest tests/e2e/test_migration.py [--skip-build] [--keep]
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "files"))


def pytest_addoption(parser):
    parser.addoption("--skip-build", action="store_true", help="use the images that exist; do not build")
    parser.addoption("--keep", action="store_true", help="leave the stack running after the tests")
    parser.addoption("--db-engine", choices=("mariadb", "postgres"), default="mariadb",
                     help="the database the integration suite runs on")
    parser.addoption("--url", default="", help="the MISP the smoke test checks, e.g. https://misp.example.com")
    parser.addoption("--key", default="", help="an API key for the smoke test's authenticated checks")


@pytest.fixture(scope="session")
def options(request):
    return {"skip_build": request.config.getoption("--skip-build"),
            "keep": request.config.getoption("--keep"),
            "db_engine": request.config.getoption("--db-engine"),
            "url": request.config.getoption("--url").rstrip("/"),
            "key": request.config.getoption("--key")}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """At a module's first failure, dump every service's log.

    The dump at the end of a module misses a container that a later step
    removed, such as a web replica after a scale-down.
    """
    outcome = yield
    report = outcome.get_result()
    stack = getattr(item, "funcargs", {}).get("stack")
    if report.failed and stack is not None and stack.files and not getattr(stack, "dumped", False):
        stack.dumped = True
        suite = item.module.__name__.removeprefix("test_")
        report.sections.append(("compose logs", str(stack.dump_logs(f"{suite}-first-failure"))))


@pytest.fixture(scope="module")
def failed_in_module(request):
    """A callable: True when a test of this module has failed so far."""
    before = request.session.testsfailed
    return lambda: request.session.testsfailed > before
