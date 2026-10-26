"""A single-node k3s cluster in a privileged container, for the stack suites.

K3S_CMD runs the cluster: by default podman on the machine's rootful connection
on macOS (k3s needs privileges that rootless podman does not give), and
CONTAINER_CMD elsewhere, docker in CI. CONTAINER_CMD holds the images under test
(podman by default); they reach the cluster's containerd through save and import.
Each port pair publishes a NodePort of the cluster on localhost.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

K3S_IMAGE = ("docker.io/rancher/k3s:v1.37.1-k3s1"
             "@sha256:ca7f37d993d82ef0dcdcfecb2e0e2618ea541dbaffc620c8cedebe01a82acd0d")
REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "deploy" / "chart"
IMAGES = tuple(yaml.safe_load((CHART / "values.yaml").read_text())["image"][name]
               for name in ("misp", "modules"))

sys.path.insert(0, str(REPO / "scripts"))
# The tag of the images under test: MISP_IMAGE_TAG, else the chart's appVersion
from build import default_tag as image_tag  # noqa: E402


def scratch_dir() -> Path:
    """The physical TMPDIR: a podman machine on macOS shares /private, not the /tmp symlink."""
    return Path(os.environ.get("TMPDIR", "/tmp")).resolve()


def image_engine() -> list[str]:
    return shlex.split(os.environ.get("CONTAINER_CMD", "podman"))


def cluster_engine() -> list[str]:
    if os.environ.get("K3S_CMD"):
        return shlex.split(os.environ["K3S_CMD"])
    if platform.system() == "Darwin" and image_engine()[0].endswith("podman"):
        return ["podman", "--connection", "podman-machine-default-root"]
    return image_engine()


def build_images(options: dict, _built: list[str] = []) -> None:
    """The images under test, through scripts/build.py, once a run, unless it passed --skip-build."""
    if not options["skip_build"] and image_tag() not in _built:
        subprocess.run([sys.executable, str(REPO / "scripts" / "build.py"), image_tag()], check=True, timeout=3600)
        _built.append(image_tag())


class Cluster:
    def __init__(self, name: str, ports: dict[int, int] | None = None, api_port: int = 16443):
        """ports maps a host port to a NodePort (30000-32767) of the cluster."""
        self.name = name
        self.ports = ports or {}
        self.api_port = api_port
        self.kubeconfig = scratch_dir() / f"{name}.kubeconfig"
        self.engine = cluster_engine()

    # -- lifecycle -----------------------------------------------------------------

    def start(self, timeout: int = 180) -> None:
        self.delete()
        published = [arg for host, node in self.ports.items() for arg in ("-p", f"{host}:{node}")]
        subprocess.run([*self.engine, "run", "-d", "--name", self.name, "--privileged",
                        "-p", f"{self.api_port}:6443", *published, K3S_IMAGE, "server",
                        "--disable", "traefik", "--disable", "servicelb", "--disable", "metrics-server"],
                       check=True, capture_output=True, text=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            nodes = self.node_exec("kubectl", "get", "nodes", "--no-headers")
            if nodes.returncode == 0 and " Ready " in nodes.stdout:
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"k3s {self.name} has no Ready node after {timeout}s")
        config = self.node_exec("cat", "/etc/rancher/k3s/k3s.yaml").stdout
        self.kubeconfig.write_text(config.replace("https://127.0.0.1:6443", f"https://127.0.0.1:{self.api_port}"))
        # The default service account exists a moment after the node is Ready
        deadline = time.monotonic() + 60
        while self.kubectl("get", "serviceaccount", "default").returncode != 0 and time.monotonic() < deadline:
            time.sleep(1)

    def delete(self) -> None:
        subprocess.run([*self.engine, "rm", "-f", "-v", self.name], capture_output=True)
        self.kubeconfig.unlink(missing_ok=True)

    def node_exec(self, *command: str, input: bytes | None = None) -> subprocess.CompletedProcess:
        interactive = ["-i"] if input is not None else []
        return subprocess.run([*self.engine, "exec", *interactive, self.name, *command],
                              input=input, capture_output=True, text=input is None)

    def load(self, images: list[str]) -> None:
        """Copy images from CONTAINER_CMD's store into the cluster's containerd."""
        for image in images:
            save = subprocess.Popen([*image_engine(), "save", image], stdout=subprocess.PIPE)
            loaded = subprocess.run([*self.engine, "exec", "-i", self.name, "ctr", "images", "import", "-"],
                                    stdin=save.stdout, capture_output=True, text=True)
            save.stdout.close()
            if save.wait() != 0 or loaded.returncode != 0:
                raise RuntimeError(f"cannot load {image} into {self.name}: {loaded.stderr}")

    def load_images_under_test(self) -> str:
        """The three images under the tag under test; returns that tag."""
        tag = image_tag()
        self.load([f"{repo}:{tag}" for repo in IMAGES])
        return tag

    # -- clients ---------------------------------------------------------------------

    def env(self) -> dict:
        return {**os.environ, "KUBECONFIG": str(self.kubeconfig)}

    def kubectl(self, *args: str, input: str | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
        return subprocess.run(["kubectl", *args], input=input, capture_output=True, text=True, env=self.env(),
                              timeout=timeout)

    def helm(self, *args: str, timeout: int = 1200) -> subprocess.CompletedProcess:
        result = subprocess.run(["helm", *args], capture_output=True, text=True, env=self.env(), timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"helm {' '.join(args)} exited {result.returncode}:\n{result.stdout}\n{result.stderr}")
        return result

    def dump_logs(self, name: str) -> Path:
        """The state of every pod outside kube-system and the log of each of its containers, in one file."""
        path = scratch_dir() / f"{name}-cluster-logs.txt"
        out = [self.kubectl("get", "pods,jobs,events", "-A", "-o", "wide").stdout]
        pods = json.loads(self.kubectl("get", "pods", "-A", "-o", "json").stdout or '{"items": []}')["items"]
        for pod in pods:
            namespace, pod_name = pod["metadata"]["namespace"], pod["metadata"]["name"]
            if namespace == "kube-system":
                continue
            for container in [*pod["spec"].get("initContainers", []), *pod["spec"]["containers"]]:
                logs = self.kubectl("logs", "-n", namespace, pod_name, "-c", container["name"], timeout=120)
                out.append(f"--- {namespace}/{pod_name} {container['name']} ---\n{logs.stdout}{logs.stderr}")
        path.write_text("\n".join(out))
        return path

    def finish(self, name: str, keep: bool, failed: bool) -> None:
        """Dump the logs after a failure, then delete the cluster unless keep."""
        if failed:
            print(f"\ncluster logs: {self.dump_logs(name)}")
        if keep:
            print(f"\ncluster kept: KUBECONFIG={self.kubeconfig} kubectl get pods -A")
        else:
            self.delete()

    def apply(self, *documents: dict, namespace: str | None = None) -> None:
        args = ["apply", "-f", "-"] + (["-n", namespace] if namespace else [])
        result = self.kubectl(*args, input=yaml.safe_dump_all(documents))
        if result.returncode != 0:
            raise RuntimeError(f"kubectl apply failed:\n{result.stderr}")
