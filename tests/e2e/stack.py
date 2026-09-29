"""A Compose stack under test: compose calls, container exec, SQL, HTTP and waits.

COMPOSE_CMD selects the compose runner (default "podman compose") and
CONTAINER_CMD the engine behind it (default "podman"). The engine runs exec
and logs directly: it costs a third of a compose call, which parses every
compose file each time.

podman mounts a writable tmpfs on /tmp, /run and /var/tmp of a read-only
container; docker and Kubernetes (readOnlyRootFilesystem) do not. Under
podman-compose the stack turns that off, so a local run fails on the same
writes as CI and a cluster.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
TESTS = REPO / "tests"
# podman-compose prints the ID of every container it creates or starts
CONTAINER_ID_LINE = re.compile(r"^[0-9a-f]{64}$")
PODMAN_STRICT_READ_ONLY = "--podman-run-args=--read-only-tmpfs=false"


def scratch_dir() -> Path:
    """The physical TMPDIR: a podman machine on macOS shares /private, not the /tmp symlink."""
    return Path(os.environ.get("TMPDIR", "/tmp")).resolve()


def env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of an env file; the last value of a key wins."""
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Stack:
    def __init__(self, files: list[Path], profiles: tuple[str, ...] = (), port: int = 28080):
        self.files = [Path(f) for f in files]
        self.profiles = profiles
        # Compose names the project after the directory of the first compose file
        self.project = self.files[0].resolve().parent.name if self.files else ""
        self.base_url = f"http://localhost:{port}"
        self.runner = shlex.split(os.environ.get("COMPOSE_CMD", "podman compose"))
        if "podman" in Path(self.runner[0]).name:
            self.runner.append(PODMAN_STRICT_READ_ONLY)
        self.engine = os.environ.get("CONTAINER_CMD", "podman")
        self._ids: dict[str, str] = {}

    # -- compose ---------------------------------------------------------------

    def compose_args(self, extra_files=(), profiles=None) -> list[str]:
        args = []
        for f in [*self.files, *extra_files]:
            args += ["-f", str(f)]
        for p in self.profiles if profiles is None else profiles:
            args += ["--profile", p]
        return args

    def compose(self, *args, extra_files=(), profiles=None, check=False, timeout=900) -> subprocess.CompletedProcess:
        cmd = [*self.runner, *self.compose_args(extra_files, profiles), *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        result.stdout = "\n".join(l for l in result.stdout.splitlines() if not CONTAINER_ID_LINE.match(l))
        if check and result.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} exited {result.returncode}:\n{result.stdout}\n{result.stderr}")
        if args and args[0] in ("up", "run", "down", "stop", "start", "restart", "rm"):
            self._ids.clear()
        return result

    def run(self, service: str, *command: str, env: dict | None = None, volumes=(), timeout=900) -> tuple[int, str]:
        """compose run --rm --no-deps: the exit code and the combined output."""
        args = ["run", "--rm", "-T", "--no-deps"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        for volume in volumes:
            args += ["-v", volume]
        result = self.compose(*args, service, *command, timeout=timeout)
        return result.returncode, result.stdout + result.stderr

    # -- containers ------------------------------------------------------------

    def container_ids(self, service: str, stopped: bool = False) -> list[str]:
        """Every container of a service, the exited ones too when stopped is set."""
        cmd = [self.engine, "ps", "-q", "--filter", f"label=com.docker.compose.project={self.project}",
               "--filter", f"label=com.docker.compose.service={service}"]
        if stopped:
            cmd.insert(2, "-a")
        return subprocess.run(cmd, capture_output=True, text=True).stdout.split()

    def container_id(self, service: str) -> str:
        if service not in self._ids:
            ids = self.container_ids(service)
            if not ids:
                raise RuntimeError(f"no running container for service {service}")
            self._ids[service] = ids[0]
        return self._ids[service]

    def exec_rc(self, service: str, script: str, env: dict | None = None) -> tuple[int, str]:
        """Run a shell script in a service's container: the exit code and stdout plus stderr."""
        cmd = [self.engine, "exec"]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [self.container_id(service), "sh", "-c", script]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode, (result.stdout + result.stderr).strip()

    def exec(self, service: str, script: str, env: dict | None = None) -> str:
        """Run a shell script in a service's container; returns stdout, stripped."""
        cmd = [self.engine, "exec"]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [self.container_id(service), "sh", "-c", script]
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()

    def python(self, code: str, service: str = "web", env: dict | None = None) -> tuple[int, str]:
        """Python in a service's container with the image's library importable.

        The code travels in an environment variable, so it needs no shell quoting.
        """
        prelude = "import sys; sys.path.insert(0, '/opt')\n"
        return self.exec_rc(service, 'python3 -c "$CODE"', env={**(env or {}), "CODE": prelude + code})

    def sql(self, query: str, service: str = "web") -> str:
        """A query through the image's db layer, against the database the service uses."""
        _, out = self.python("import os\nfrom misp_container.env import apply_defaults\n"
                             "from misp_container import db\napply_defaults()\n"
                             "print(db.query(os.environ['SQL']))", service, env={"SQL": query})
        return "".join(out.split())

    def logs(self, service: str, stopped: bool = False) -> str:
        """The logs of every container of a service."""
        out = ""
        for cid in self.container_ids(service, stopped):
            result = subprocess.run([self.engine, "logs", cid], capture_output=True, text=True)
            out += result.stdout + result.stderr
        return out

    # -- HTTP ------------------------------------------------------------------

    def http(self, method: str, path: str, key: str = "", data=None, timeout: int = 60,
             json_api: bool = True, base_url: str = "", headers: dict | None = None,
             redirects: bool = True) -> tuple[int, str]:
        """One request to MISP through caddy: the status and the body.

        MISP answers a JSON request for a page such as /users/login with 403,
        so a page check sets json_api=False. With redirects off, a redirect
        comes back as its own status.
        """
        sent = {"Accept": "application/json", "Content-Type": "application/json"} if json_api else {}
        sent.update(headers or {})
        if key:
            sent["Authorization"] = key
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request((base_url or self.base_url) + path, data=body, method=method,
                                         headers=sent)
        opener = urllib.request.build_opener() if redirects else urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            return 0, str(e)

    def api(self, method: str, path: str, key: str, data=None, base_url: str = ""):
        """A JSON API call; None when the answer is not JSON."""
        _, body = self.http(method, path, key, data, base_url=base_url)
        try:
            return json.loads(body)
        except ValueError:
            return None

    # -- waits -----------------------------------------------------------------

    def wait_for_misp(self, timeout: int = 300, base_url: str = "") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, _ = self.http("GET", "/users/login", timeout=10, json_api=False, base_url=base_url)
            if status == 200:
                return
            time.sleep(3)
        raise RuntimeError(f"MISP did not answer on {base_url or self.base_url} in {timeout}s")

    def wait_for(self, service: str, script: str, timeout: int = 90) -> None:
        """Until a shell check in the service's container succeeds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                cid = self.container_id(service)
                if subprocess.run([self.engine, "exec", cid, "sh", "-c", script],
                                  capture_output=True).returncode == 0:
                    return
            except RuntimeError:
                self._ids.pop(service, None)
            time.sleep(3)
        raise RuntimeError(f"{service} did not pass '{script}' in {timeout}s")

    def admin_key(self, email: str) -> str:
        """A fresh authkey for the admin, set through MISP's console."""
        out = self.exec("web", f"/var/www/MISP/app/Console/cake user change_authkey {shlex.quote(email)} 2>&1")
        match = re.search(r"[A-Za-z0-9]{40}", out)
        if not match:
            raise RuntimeError(f"no authkey in: {out}")
        return match.group(0)

    def dump_logs(self, name: str) -> Path:
        """Every service's log into one file under the scratch directory."""
        path = scratch_dir() / f"{name}-compose-logs.txt"
        result = self.compose("logs", timeout=300)
        path.write_text(result.stdout + result.stderr)
        return path

    def finish(self, name: str, keep: bool, failed: bool) -> None:
        """Dump the logs after a failure, then tear the stack down unless keep."""
        if failed:
            print(f"\ncompose logs: {self.dump_logs(name)}")
        if keep:
            print(f"\nstack kept: {' '.join(self.runner)} {' '.join(self.compose_args())} ps")
        else:
            self.compose("down", "-v", timeout=600)
