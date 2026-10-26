"""Relay the log files MISP writes directly to stdout, and keep them small.

Usage: python3 -m misp_container.logrelay

MISP appends some logs straight to files under app/tmp/logs instead of
through CakeLog: every server sync request (server-sync.log), workflow debug
output, failed external commands, Kafka errors, the fatal errors a request
hits, and the output of the ZeroMQ publisher. In a pod those files reach no log
collector and grow until the volume fills. The relay follows each file, writes
every new line to stdout in the container's log format, and empties a file once
it passes MAX_BYTES. It keeps its offsets in OFFSETS, so a restart of the
container relays no line twice.
"""

import json
import os
import sys
import time
from pathlib import Path

from . import MISP_BASE
from .log import setup as setup_logging, get as getlog

LOG_DIR = Path(MISP_BASE) / "app/tmp/logs"
# On the pod's app/tmp volume, beside the logs: the guard counts every file in logs/ as MISP's
OFFSETS = Path(MISP_BASE) / "app/tmp/logrelay-offsets.json"
FILES = ("server-sync.log", "workflow-execution.log", "exec-errors.log", "kafka.error.log",
         "fatal_error.log", "mispzmq.log", "mispzmq.error.log")
MAX_BYTES = 10 * 1024 * 1024
INTERVAL = 1.0


class Relay:
    """Follows the files in a directory; one instance per process."""

    def __init__(self, directory: Path = LOG_DIR, files=FILES, max_bytes: int = MAX_BYTES, emit=None,
                 offsets_file: Path | None = OFFSETS):
        self.directory = Path(directory)
        self.files = files
        self.max_bytes = max_bytes
        self.offsets_file = offsets_file
        self.offsets: dict[str, int] = self._load_offsets()
        self.partial: dict[str, str] = {}
        self.emit = emit or self._log

    def _load_offsets(self) -> dict[str, int]:
        try:
            return {k: int(v) for k, v in json.loads(self.offsets_file.read_text()).items()}
        except (AttributeError, OSError, ValueError):
            return {}

    def _save_offsets(self) -> None:
        if self.offsets_file is not None:
            temp = self.offsets_file.with_suffix(".tmp")
            temp.write_text(json.dumps(self.offsets))
            temp.replace(self.offsets_file)

    @staticmethod
    def _log(name: str, line: str) -> None:
        getlog(f"misp:{name.removesuffix('.log')}").info("%s", line)

    def poll(self) -> int:
        """Relay what the files gained since the last poll. Returns the lines relayed."""
        relayed = 0
        before = dict(self.offsets)
        for name in self.files:
            path = self.directory / name
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                self.offsets.pop(name, None)
                continue
            offset = self.offsets.get(name, 0)
            if size < offset:
                # Emptied by someone else: start over
                offset = 0
            if size > offset:
                with open(path, "rb") as f:
                    f.seek(offset)
                    data = f.read(size - offset)
                offset += len(data)
                text = self.partial.pop(name, "") + data.decode("utf-8", errors="replace")
                lines = text.split("\n")
                if lines[-1]:
                    self.partial[name] = lines[-1]
                for line in lines[:-1]:
                    if line.strip():
                        self.emit(name, line)
                        relayed += 1
            if offset > self.max_bytes:
                # A line MISP appends between the read and this truncate is lost
                with open(path, "r+b") as f:
                    f.truncate(0)
                offset = 0
            self.offsets[name] = offset
        if self.offsets != before:
            self._save_offsets()
        return relayed

    def run(self, interval: float = INTERVAL) -> None:
        while True:
            self.poll()
            time.sleep(interval)


def main() -> None:
    setup_logging("logrelay")
    os.makedirs(LOG_DIR, exist_ok=True)
    try:
        Relay().run()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
