"""Thin kubectl wrapper. Every call is pinned to the lab's own kind context, so it can
never touch another cluster that happens to be in the kubeconfig."""
import json
import os
import shutil
import subprocess
import time

CLUSTER = "rollout-lab"
CONTEXT = f"kind-{CLUSTER}"
NAMESPACE = "rollout-lab"
IMAGE = "rollout-app:1.0.0"
APP_SELECTOR = "app.kubernetes.io/name=rollout-app,role=server"


class KubectlError(RuntimeError):
    pass


def find(binary, env_name):
    path = os.environ.get(env_name) or shutil.which(binary)
    if not path:
        raise FileNotFoundError(f"{binary} not found: put it on PATH or set {env_name}")
    return path


class Kube:
    def __init__(self, namespace=NAMESPACE, context=CONTEXT):
        self.kubectl = find("kubectl", "KUBECTL")
        self.namespace = namespace
        self.context = context

    def base_cmd(self, namespaced=True):
        cmd = [self.kubectl, "--context", self.context]
        if namespaced:
            cmd += ["-n", self.namespace]
        return cmd

    def run(self, *args, check=True, timeout=120, input=None, namespaced=True):
        proc = subprocess.run(self.base_cmd(namespaced) + list(args), capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout, input=input)
        if check and proc.returncode != 0:
            raise KubectlError(f"kubectl {' '.join(args)} failed ({proc.returncode}): "
                               f"{(proc.stderr or proc.stdout).strip()}")
        return proc

    def json(self, *args, **kw):
        return json.loads(self.run(*args, "-o", "json", **kw).stdout)

    def reachable(self):
        try:
            return self.run("version", "-o", "json", namespaced=False, timeout=15,
                            check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def apply_k(self, path, *extra, check=True):
        """apply -k is idempotent, so a hung call is killed and tried once more. (A single
        `apply -k` hung for 120 s during one long run; the retry is a robustness fix and
        changes nothing that is measured.)"""
        for attempt in (1, 2):
            try:
                return self.run("apply", "-k", str(path), *extra, namespaced=False, check=check, timeout=60)
            except subprocess.TimeoutExpired:
                if attempt == 2:
                    raise
                print("kubectl apply -k hung for 60 s, retrying once", flush=True)

    # ---- readiness helpers -------------------------------------------------------------
    def server_pods(self):
        return self.json("get", "pods", "-l", APP_SELECTOR)["items"]

    @staticmethod
    def pod_ready(pod):
        if pod["metadata"].get("deletionTimestamp"):
            return False
        return any(c["type"] == "Ready" and c["status"] == "True"
                   for c in pod.get("status", {}).get("conditions", []))

    def ready_count(self):
        return sum(1 for p in self.server_pods() if self.pod_ready(p))

    def wait_ready(self, want, timeout=90):
        """Wait until exactly `want` serving pods are ready and no others are terminating."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pods = self.server_pods()
            if len(pods) == want and all(self.pod_ready(p) for p in pods):
                return True
            time.sleep(0.5)
        raise TimeoutError(f"{want} ready pods not reached in {timeout}s")

    def rollout_status(self, timeout=120):
        """Block until the Deployment's rollout finishes. Returns seconds taken."""
        t = time.monotonic()
        self.run("rollout", "status", "deploy/app", f"--timeout={timeout}s", timeout=timeout + 15)
        return time.monotonic() - t

    def deployment(self):
        return self.json("get", "deploy", "app")


def deploy_variant(kube, path, replicas=3):
    """Clean slate: remove the Deployment and PDB, apply the variant, wait until ready."""
    kube.run("delete", "deploy/app", "pdb/app", "--ignore-not-found", "--wait=true", timeout=120)
    kube.apply_k(path)
    kube.wait_ready(replicas, timeout=90)
    time.sleep(6)  # endpoints settle, and the app finishes its startup delay even without a readiness probe
