"""Unit tests for the worker supervisor (misp_container.worker): the programs, restarts and stop."""

import os
import signal
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import CAKE, MISP_BASE, worker


class FakeProc:
    """A child that exits when told to, or on its own with ignores_term off."""

    def __init__(self, pid, ignores_term=False):
        self.pid = pid
        self.returncode = None
        self.ignores_term = ignores_term

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


def program(name="default_00", proc=None, started=0.0):
    p = worker.Program(name, ["true"])
    p.proc, p.started = proc, started
    return p


class TestPrograms:
    def test_one_program_per_worker_process_then_the_log_relay(self):
        counts = {"NUM_WORKERS_DEFAULT": "2", "NUM_WORKERS_PRIO": "0", "NUM_WORKERS_EMAIL": "0",
                  "NUM_WORKERS_UPDATE": "1", "NUM_WORKERS_CACHE": "0"}
        with patch.dict(os.environ, counts):
            programs = worker.programs()
        assert [p.name for p in programs] == ["default_00", "default_01", "update_00", "logrelay"]
        assert programs[0].command == [CAKE, "start_worker", "default"] and programs[0].cwd == MISP_BASE
        assert programs[-1].command[1:] == ["-m", "misp_container.logrelay"]


class TestRestarts:
    def test_a_worker_that_ran_its_course_restarts_at_once(self):
        p = program(proc=FakeProc(10), started=0.0)
        p.proc.returncode = 0
        worker.Supervisor([p], grace=5).reap(now=3600.0)
        assert (p.proc, p.failures, p.due) == (None, 0, 3600.0)

    def test_a_process_that_dies_at_once_waits_longer_each_time(self):
        p = program()
        sup = worker.Supervisor([p], grace=5)
        delays = []
        for attempt in range(8):
            p.proc, p.started = FakeProc(10 + attempt), 100.0 * attempt
            p.proc.returncode = 255
            sup.reap(now=100.0 * attempt + 1)
            delays.append(p.due - (100.0 * attempt + 1))
        assert delays == [1, 2, 4, 8, 16, 32, worker.MAX_DELAY, worker.MAX_DELAY]

    def test_a_long_run_resets_the_delay(self):
        p = program(proc=FakeProc(10), started=0.0)
        p.failures = 5
        p.proc.returncode = 0
        worker.Supervisor([p], grace=5).reap(now=worker.RAPID_EXIT + 1)
        assert p.failures == 0

    def test_only_programs_that_are_due_start(self):
        due, later = program("a"), program("b")
        later.due = 50.0
        sup = worker.Supervisor([due, later], grace=5)
        with patch.object(worker.Program, "start", autospec=True) as start:
            sup.start_due(now=10.0)
        assert [call.args[0].name for call in start.call_args_list] == ["a"]


class TestStop:
    def test_each_worker_gets_sigterm_and_the_groups_of_stragglers_sigkill(self):
        # SIGTERM goes to the worker alone: its running job, in its group, runs on
        polite, busy = FakeProc(21), FakeProc(22, ignores_term=True)
        procs = {21: polite, 22: busy}
        sent = []

        def kill(pid, signum):
            sent.append(("kill", pid, signum))
            if not procs[pid].ignores_term:
                procs[pid].returncode = -signum

        def killpg(pid, signum):
            sent.append(("killpg", pid, signum))
            procs[pid].returncode = -signum

        sup = worker.Supervisor([program("a", polite), program("b", busy)], grace=0.2)
        with patch.object(worker.os, "kill", side_effect=kill), patch.object(worker.os, "killpg", side_effect=killpg), \
                patch.object(worker, "POLL", 0.05):
            assert sup.shutdown() == 0
        assert sent == [("kill", 21, signal.SIGTERM), ("kill", 22, signal.SIGTERM), ("killpg", 22, signal.SIGKILL)]

    def test_a_process_that_is_already_gone_is_no_error(self):
        proc = FakeProc(31)
        sup = worker.Supervisor([program("a", proc)], grace=0.1)

        def kill(pid, signum):
            proc.returncode = 0
            raise ProcessLookupError

        with patch.object(worker.os, "kill", side_effect=kill):
            assert sup.shutdown() == 0
