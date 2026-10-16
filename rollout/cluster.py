"""Create and delete the local kind cluster, and get the app image into it."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .kube import CLUSTER, IMAGE, NAMESPACE, Kube, find

ROOT = Path(__file__).resolve().parent.parent


def _run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], text=True, **kw)


def kind_bin():
    return find("kind", "KIND")


def cluster_exists():
    out = subprocess.run([kind_bin(), "get", "clusters"], capture_output=True, text=True)
    return CLUSTER in out.stdout.split()


def build_image():
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    proc = _run(["docker", "build", "-t", IMAGE, "-f", ROOT / "app" / "Dockerfile", ROOT], env=env)
    if proc.returncode:
        raise RuntimeError("docker build failed")


def load_image():
    """`kind load docker-image` first; fall back to an image archive, which also works when
    Docker's containerd image store is enabled."""
    proc = _run([kind_bin(), "load", "docker-image", IMAGE, "--name", CLUSTER])
    if proc.returncode == 0:
        return "docker-image"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "image.tar"
        if _run(["docker", "save", "-o", archive, IMAGE]).returncode:
            raise RuntimeError("docker save failed")
        if _run([kind_bin(), "load", "image-archive", archive, "--name", CLUSTER]).returncode:
            raise RuntimeError("kind load image-archive failed")
    return "image-archive"


def up():
    if not cluster_exists():
        if _run([kind_bin(), "create", "cluster", "--name", CLUSTER, "--config",
                 ROOT / "kind-config.yaml", "--wait", "180s"]).returncode:
            raise RuntimeError("kind create cluster failed")
    build_image()
    print("image loaded via", load_image())
    kube = Kube()
    kube.run("apply", "-k", str(ROOT / "deploy" / "overlays" / "dev"), namespaced=False)
    print(f"cluster {CLUSTER} ready, namespace {NAMESPACE} created")


def down():
    if cluster_exists():
        _run([kind_bin(), "delete", "cluster", "--name", CLUSTER])
    else:
        print("no such cluster:", CLUSTER)


def versions():
    """Versions that go into results/summary.json."""
    kube = Kube()
    server = json.loads(kube.run("version", "-o", "json", namespaced=False).stdout)
    nodes = kube.json("get", "nodes", namespaced=False)["items"]
    return {
        "kind": subprocess.run([kind_bin(), "version"], capture_output=True, text=True).stdout.strip(),
        "kubectl_client": server["clientVersion"]["gitVersion"],
        "kubernetes_server": server["serverVersion"]["gitVersion"],
        "node_image": next(iter({n["status"]["nodeInfo"]["osImage"] for n in nodes}), None),
        "nodes": [n["metadata"]["name"] for n in nodes],
        "container_runtime": nodes[0]["status"]["nodeInfo"]["containerRuntimeVersion"],
        "docker": subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                                 capture_output=True, text=True).stdout.strip(),
        "python": sys.version.split()[0],
    }
