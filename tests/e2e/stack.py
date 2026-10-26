"""A release of the chart under test: helm, pod exec, SQL, HTTP and waits.

A Release lives in one namespace of a Cluster (cluster.py) and takes the
values of tests/k3s/values.yaml, then the suite's own. The host reaches its web
Service through a NodePort the cluster publishes. One-off commands, such as an
org sync, a migration or a task, run in the toolbox: a pod from the pod
template of the release's configure Job, so with its image, env and mounts.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

import manifests
from cluster import CHART, IMAGES, REPO, Cluster, image_engine, image_tag, scratch_dir

TESTS = REPO / "tests"
K3S = TESTS / "k3s"
VALUES = K3S / "values.yaml"
# A name the suites use for a workload of the chart: (the object kubectl execs into, the container).
# Any other name is a Deployment with a container of that name, such as a test service
WORKLOADS = {
    "web": ("deploy/web", "php-fpm"),
    "caddy": ("deploy/web", "caddy"),
    "worker": ("deploy/worker", "worker"),
    "metrics": ("deploy/metrics", "metrics"),
    "modules": ("deploy/modules", "modules"),
    "postgres": ("statefulset/postgres", "postgres"),
    "redis": ("deploy/redis", "redis"),
    "toolbox": ("pod/toolbox", "toolbox"),
}
LABELS = {"caddy": "web"}


def values_file(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def merge(base: dict, extra: dict) -> dict:
    """extra over base, map by map, as helm merges values files."""
    merged = dict(base)
    for key, value in extra.items():
        merged[key] = merge(merged[key], value) if isinstance(value, dict) and isinstance(merged.get(key), dict) \
            else value
    return merged


def gnupg_key(email: str, passphrase: str) -> dict:
    """The Secret misp-gnupg with a new instance key, made by gpg in the image under test."""
    script = ('export GNUPGHOME="$(mktemp -d)" && '
              'gpg --batch --pinentry-mode loopback --passphrase "$P" '
              '--quick-generate-key "MISP Admin <$E>" rsa3072 sign never && '
              'gpg --batch --pinentry-mode loopback --passphrase "$P" --armor --export-secret-keys "$E"')
    result = subprocess.run([*image_engine(), "run", "--rm", "-e", f"P={passphrase}", "-e", f"E={email}",
                             "--entrypoint", "sh", f"{IMAGES[0]}:{image_tag()}", "-c", script],
                            capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or "PRIVATE KEY BLOCK" not in result.stdout:
        raise RuntimeError(f"gpg made no key: {result.stderr}")
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "misp-gnupg"},
            "stringData": {"private.asc": result.stdout}}


def server_certificate(*names: str) -> dict[str, str]:
    """A CA and a server certificate for these DNS names, by openssl in the test PostgreSQL's image.

    Returns the keys of a TLS Secret: ca.crt, tls.crt and tls.key.
    """
    script = ('cd "$(mktemp -d)" && '
              'openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj /CN=misp-test-ca '
              '-addext keyUsage=critical,keyCertSign,cRLSign -keyout ca.key -out ca.crt 2>/dev/null && '
              'openssl req -newkey rsa:2048 -nodes -subj "/CN=$CN" -keyout tls.key -out tls.csr 2>/dev/null && '
              'printf "subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,'
              'keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n'
              'authorityKeyIdentifier=keyid\n" "$SAN" > ext && '
              'openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 7 -extfile ext '
              '-out tls.crt 2>/dev/null && cat ca.crt tls.crt tls.key')
    san = ",".join(f"DNS:{name}" for name in names)
    result = subprocess.run([*image_engine(), "run", "--rm", "-e", f"CN={names[0]}", "-e", f"SAN={san}",
                             "--entrypoint", "sh", manifests.image("postgres-tls"), "-c", script],
                            capture_output=True, text=True, timeout=300)
    blocks = [(m.group(1), m.group(0)) for m in
              re.finditer(r"-----BEGIN ([A-Z ]+)-----\n.*?\n-----END \1-----\n", result.stdout, re.S)]
    certificates = [block for kind, block in blocks if kind == "CERTIFICATE"]
    keys = [block for kind, block in blocks if kind.endswith("PRIVATE KEY")]
    if result.returncode != 0 or len(certificates) != 2 or len(keys) != 1:
        raise RuntimeError(f"openssl made no certificate: {result.stderr}")
    return {"ca.crt": certificates[0], "tls.crt": certificates[1], "tls.key": keys[0]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Misp:
    """A MISP by its URL: requests to its web tier."""

    def __init__(self, base_url: str, key: str = ""):
        self.base_url = base_url
        self.key = key

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


class Release(Misp):
    def __init__(self, cluster: Cluster, namespace: str = "misp", port: int = 28080, values: dict | None = None):
        """values go on top of tests/k3s/values.yaml; port is the host port of the web NodePort."""
        super().__init__(f"http://localhost:{port}")
        self.cluster = cluster
        self.namespace = namespace
        self.values = merge(values_file(VALUES), values or {})
        self.revision = 0
        self.dumped = False
        self._toolbox = ""

    # -- helm ------------------------------------------------------------------------

    def install(self, values: dict | None = None, chart: str = str(CHART), version: str = "",
                tag: str | None = None, wait: bool = True) -> None:
        """helm upgrade --install with the values so far plus these, then wait for the revision.

        values stay for later upgrades. The images run under tag, by default the
        tag under test; an empty tag is the chart's appVersion. Without wait, call
        wait_for_revision() before the release is used.
        """
        self.values = merge(self.values, values or {})
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", dir=scratch_dir(), delete=False) as f:
            yaml.safe_dump(merge(self.values, {"image": {"tag": image_tag() if tag is None else tag}}), f)
        try:
            self.cluster.helm("upgrade", "--install", "misp", chart, "-n", self.namespace, "--create-namespace",
                              "-f", f.name, *(["--version", version] if version else []))
        finally:
            Path(f.name).unlink()
        self.revision = json.loads(self.cluster.helm("status", "misp", "-n", self.namespace, "-o", "json")
                                   .stdout)["version"]
        if wait:
            self.wait_for_revision()

    def rollback(self, revision: int) -> None:
        """helm rollback, then wait: helm applies the manifest of that revision again, so its Jobs too."""
        self.cluster.helm("rollback", "misp", str(revision), "-n", self.namespace)
        self.revision = revision
        self.wait_for_revision()

    def wait_for_revision(self, timeout: int = 900) -> None:
        """The configure Job of the revision, the Deployments, then its org-sync Job.

        helm waits for none of them: the Jobs carry Argo CD annotations, not Helm hooks.
        """
        self._toolbox = ""
        self.wait_for_job(f"configure-{self.revision}", timeout)
        for deploy in ("web", "worker", "modules", "metrics"):
            self.rollout(deploy)
        self.wait_for_job(f"org-sync-{self.revision}", 300)

    def wait_for_job(self, job: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.kubectl("get", "job", job, "-o",
                                 "jsonpath={.status.conditions[?(@.status==\"True\")].type}").stdout
            if "Complete" in state:
                return
            if "Failed" in state:
                break
            time.sleep(3)
        logs = self.kubectl("logs", f"job/{job}", "--all-containers", "--tail=60").stdout
        raise RuntimeError(f"job {job} did not complete in {timeout}s; logs: {self.dump_logs(job)}\n{logs}")

    def rollout(self, deploy: str, timeout: int = 600) -> None:
        result = self.kubectl("rollout", "status", f"deploy/{deploy}", f"--timeout={timeout}s", timeout=timeout + 30)
        if result.returncode != 0:
            raise RuntimeError(f"deploy/{deploy} did not roll out: {result.stderr}; logs: {self.dump_logs(deploy)}")

    def scale(self, deploy: str, replicas: int) -> None:
        """Scale a Deployment and wait: for its pods to go, or for the rollout."""
        self.kubectl("scale", f"deploy/{deploy}", f"--replicas={replicas}")
        if replicas:
            self.rollout(deploy)
        else:
            self.kubectl("wait", "--for=delete", "pod", "-l", f"app.kubernetes.io/name={deploy}", "--timeout=180s",
                         timeout=200)

    def apply(self, *documents: dict) -> None:
        """Objects in the release's namespace, which may not exist yet."""
        self.cluster.kubectl("create", "namespace", self.namespace)
        self.cluster.apply(*documents, namespace=self.namespace)

    # -- pods ------------------------------------------------------------------------

    def kubectl(self, *args: str, input: str | None = None, timeout: int = 600):
        return self.cluster.kubectl("-n", self.namespace, *args, input=input, timeout=timeout)

    def pods(self, name: str) -> list[str]:
        """The running pods of a workload, by name."""
        out = self.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={LABELS.get(name, name)}",
                           "--field-selector=status.phase=Running", "-o", "name").stdout
        return out.split()

    def _target(self, workload: str) -> list[str]:
        if workload == "toolbox":
            self.toolbox()
        target, container = WORKLOADS.get(workload, (f"deploy/{workload}", workload))
        return [target, "-c", container]

    def exec_rc(self, workload: str, script: str, env: dict | None = None, input: str | None = None,
                timeout: int = 300) -> tuple[int, str]:
        """A shell script in a workload's container: the exit code and stdout plus stderr.

        A script that outlives the timeout raises TimeoutExpired: a hang fails the test.
        """
        assignments = [f"{key}={value}" for key, value in (env or {}).items()]
        result = self.kubectl("exec", *(["-i"] if input is not None else []), *self._target(workload), "--",
                              "env", *assignments, "sh", "-c", script, input=input, timeout=timeout)
        return result.returncode, (result.stdout + result.stderr).strip()

    def exec(self, workload: str, script: str, env: dict | None = None, timeout: int = 300) -> str:
        """A shell script in a workload's container; returns stdout, stripped."""
        assignments = [f"{key}={value}" for key, value in (env or {}).items()]
        return self.kubectl("exec", *self._target(workload), "--", "env", *assignments, "sh", "-c", script,
                            timeout=timeout).stdout.strip()

    def python(self, code: str, workload: str = "web", env: dict | None = None) -> tuple[int, str]:
        """Python in a workload's container with the image's library importable.

        The code travels in an environment variable, so it needs no shell quoting.
        """
        prelude = "import sys; sys.path.insert(0, '/opt')\n"
        return self.exec_rc(workload, 'python3 -c "$CODE"', env={**(env or {}), "CODE": prelude + code})

    def sql(self, query: str, workload: str = "web") -> str:
        """A query through the image's db layer, against the database the workload uses.

        Raises when the query or the library fails: an error would otherwise
        come back as a traceback string that a negative assertion accepts.
        """
        rc, out = self.python("import os\n"
                              "from misp_container import db\n"
                              "for row in db.dict_query(os.environ['SQL']):\n"
                              "    print('\\t'.join('NULL' if v is None else str(v) for v in row.values()))",
                              workload, env={"SQL": query})
        if rc != 0:
            raise RuntimeError(f"query failed in {workload}: {query}\n{out}")
        return "".join(out.split())

    def logs(self, workload: str) -> str:
        """The logs of every pod of a workload; configure is the Job of the current revision."""
        if workload == "configure":
            return self.kubectl("logs", f"job/configure-{self.revision}", "-c", "configure").stdout
        _, container = WORKLOADS[workload]
        return self.kubectl("logs", "-l", f"app.kubernetes.io/name={LABELS.get(workload, workload)}",
                            "-c", container, "--tail=-1", "--max-log-requests=10").stdout

    def toolbox(self, container: str = "") -> None:
        """Start the toolbox from a container of the configure Job's pod template, once per revision.

        Without a container, the toolbox of this revision stays as it is, else it
        starts from the configure container.
        """
        current = self._toolbox.partition("/")
        if current[0] == str(self.revision) and container in ("", current[2]):
            return
        container = container or "configure"
        self.kubectl("delete", "pod", "toolbox", "--ignore-not-found", "--grace-period=1", timeout=120)
        job = json.loads(self.kubectl("get", "job", f"configure-{self.revision}", "-o", "json").stdout)
        self.apply(manifests.toolbox(job, container))
        result = self.kubectl("wait", "--for=condition=Ready", "pod/toolbox", "--timeout=180s", timeout=200)
        if result.returncode != 0:
            raise RuntimeError(f"the toolbox did not start: {result.stderr}")
        self._toolbox = f"{self.revision}/{container}"

    def run(self, *command: str, env: dict | None = None, files: dict[str, str] | None = None,
            container: str = "", timeout: int = 900) -> tuple[int, str]:
        """A command in the toolbox, after files {path: content} are written there: exit code and output."""
        self.toolbox(container)
        for path, content in (files or {}).items():
            rc, out = self.exec_rc("toolbox", f"mkdir -p {shlex.quote(str(Path(path).parent))} && "
                                              f"cat > {shlex.quote(path)}", input=content)
            if rc != 0:
                raise RuntimeError(f"cannot write {path} in the toolbox: {out}")
        script = " ".join(shlex.quote(arg) for arg in command)
        return self.exec_rc("toolbox", script, env=env, timeout=timeout)

    # -- waits -----------------------------------------------------------------------

    def wait_for_misp(self, timeout: int = 300, base_url: str = "") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, _ = self.http("GET", "/users/login", timeout=10, json_api=False, base_url=base_url)
            if status == 200:
                return
            time.sleep(3)
        # A fixture that fails here yields no release, so no test would dump the logs
        path = self.dump_logs("misp-wait-timeout")
        raise RuntimeError(f"MISP did not answer on {base_url or self.base_url} in {timeout}s; logs: {path}")

    def wait_for(self, workload: str, script: str, timeout: int = 90) -> None:
        """Until a shell check in the workload's container succeeds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.exec_rc(workload, script)[0] == 0:
                return
            time.sleep(3)
        raise RuntimeError(f"{workload} did not pass '{script}' in {timeout}s")

    def admin_key(self, email: str) -> str:
        """A fresh authkey for the admin, set through MISP's console."""
        out = self.exec("web", f"/var/www/MISP/app/Console/cake user change_authkey {shlex.quote(email)} 2>&1")
        match = re.search(r"[A-Za-z0-9]{40}", out)
        if not match:
            raise RuntimeError(f"no authkey in: {out}")
        return match.group(0)

    def dump_logs(self, name: str) -> Path:
        return self.cluster.dump_logs(name)
