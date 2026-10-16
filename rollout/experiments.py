"""The experiments. Each one drives kubectl against the local kind cluster and returns a
plain dict that is merged into results/summary.json. Raw per-run records go to
results/runs.jsonl so every number can be audited."""
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import stats
from .cluster import ROOT, versions
from .kube import IMAGE, NAMESPACE, Kube, KubectlError, deploy_variant

RESULTS = ROOT / "results"
PATHS = {
    "naive": ROOT / "deploy" / "variants" / "naive",
    "probes": ROOT / "deploy" / "variants" / "probes",
    "full": ROOT / "deploy" / "overlays" / "dev",
    "no-pdb": ROOT / "deploy" / "variants" / "no-pdb",
    "prod": ROOT / "deploy" / "overlays" / "prod",
}
LABELS = {
    "naive": "A: no probes, no preStop, default rolling update",
    "probes": "C: readiness + startup probes only",
    "full": "B: probes + preStop + maxUnavailable 0",
    "no-pdb": "B without the PodDisruptionBudget",
}
DEFAULT_REPS = {"rollout": 5, "badrelease": 3, "drain": 3, "netpol": 3, "validate": 1}


# ---- load generator pods -----------------------------------------------------------------

def _container_security():
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


def client_pod(name, args, role="client"):
    """A short-lived client pod running python from the app image. It is scheduled on the
    control-plane node so draining a worker never evicts it and it does not compete with
    the app pods for CPU."""
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": NAMESPACE,
                     "labels": {"role": role, "app.kubernetes.io/name": "client"}},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "nodeSelector": {"node-role.kubernetes.io/control-plane": ""},
            "tolerations": [{"key": "node-role.kubernetes.io/control-plane",
                             "operator": "Exists", "effect": "NoSchedule"}],
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "client", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                "command": ["python"] + args,
                "resources": {"requests": {"cpu": "100m", "memory": "32Mi"},
                              "limits": {"cpu": "1", "memory": "128Mi"}},
                "securityContext": _container_security(),
            }],
        },
    }


def wait_phase(kube, name, phases, timeout):
    deadline = time.monotonic() + timeout
    phase = None
    while time.monotonic() < deadline:
        proc = kube.run("get", "pod", name, "-o", "jsonpath={.status.phase}", check=False)
        phase = proc.stdout.strip()
        if phase in phases:
            return phase
        time.sleep(0.5)
    raise TimeoutError(f"pod {name} did not reach {phases} in {timeout}s (last phase {phase!r})")


def start_loadgen(kube, tag, duration, rate=40, workers=16):
    name = f"lg-{tag}".lower()
    kube.run("delete", "pod", name, "--ignore-not-found", "--wait=true")
    spec = client_pod(name, ["/srv/loadgen.py", "--url", "http://app/work", "--rate", str(rate),
                             "--duration", str(duration), "--workers", str(workers)])
    kube.run("apply", "-f", "-", input=json.dumps(spec))
    wait_phase(kube, name, {"Running", "Succeeded", "Failed"}, timeout=60)
    return name


def finish_loadgen(kube, name, duration):
    phase = wait_phase(kube, name, {"Succeeded", "Failed"}, timeout=duration + 90)
    logs = kube.run("logs", name).stdout.strip().splitlines()
    kube.run("delete", "pod", name, "--ignore-not-found", "--wait=false")
    if phase != "Succeeded" or not logs:
        raise RuntimeError(f"load generator {name} ended {phase}: {logs[-3:]}")
    return json.loads(logs[-1])


def rotate(items, k):
    """Rotate the run order each repetition so a slow drift in the machine does not always
    land on the same configuration."""
    k %= len(items)
    return items[k:] + items[:k]


def record(experiment, run):
    RESULTS.mkdir(exist_ok=True)
    with open(RESULTS / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"experiment": experiment, **run}) + "\n")


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---- experiment 1: zero-downtime rollout --------------------------------------------------

def exp_rollout(kube, reps, duration=50, trigger_at=6, rate=40):
    variants = ["naive", "probes", "full"]
    runs = []
    for rep in range(reps):
        for variant in rotate(variants, rep):
            say(f"rollout rep {rep + 1}/{reps} variant {variant}")
            deploy_variant(kube, PATHS[variant])
            lg = start_loadgen(kube, f"roll-{variant}-{rep}", duration, rate)
            time.sleep(trigger_at)
            t0 = time.monotonic()
            kube.run("rollout", "restart", "deploy/app")
            rollout_s = kube.rollout_status(timeout=120)
            summary = finish_loadgen(kube, lg, duration)
            run = {"variant": variant, "rep": rep, "rollout_seconds": rollout_s,
                   "trigger_at_s": trigger_at,
                   "rollout_covered_by_load": trigger_at + (time.monotonic() - t0) < duration,
                   "load": summary}
            runs.append(run)
            record("rollout", run)
            say(f"  failed {summary['failed']}/{summary['total']} {summary['errors']} "
                f"rollout {rollout_s:.1f}s")
    by_variant = {}
    for v in variants:
        rs = [r for r in runs if r["variant"] == v]
        by_variant[v] = {
            "label": LABELS[v],
            **stats.aggregate([r["load"] for r in rs]),
            "rollout_seconds": stats.summarise_numbers([r["rollout_seconds"] for r in rs]),
            "p99_latency_ms": stats.summarise_numbers(
                [r["load"]["latency_ms"]["p99"] for r in rs if r["load"]["latency_ms"]["p99"] is not None]),
            "all_runs_covered_rollout": all(r["rollout_covered_by_load"] for r in rs),
        }
    return {"config": {"reps": reps, "replicas": 3, "rate_rps": rate, "duration_s": duration,
                       "restart_at_s": trigger_at, "startup_delay_s": 3, "preStop_sleep_s": 5},
            "variants": by_variant}


# ---- experiment 2: bad release ------------------------------------------------------------

def _deployment_state(kube):
    d = kube.deployment()
    st, spec = d["status"], d["spec"]
    prog = next((c for c in st.get("conditions", []) if c["type"] == "Progressing"), {})
    want = spec["replicas"]
    complete = (d["metadata"]["generation"] <= st.get("observedGeneration", 0)
                and st.get("updatedReplicas") == want and st.get("replicas") == want
                and st.get("availableReplicas") == want)
    return {"complete": complete, "deadline_exceeded": prog.get("reason") == "ProgressDeadlineExceeded",
            "ready": st.get("readyReplicas", 0), "available": st.get("availableReplicas", 0),
            "updated": st.get("updatedReplicas", 0)}


def exp_badrelease(kube, reps, duration=75, trigger_at=6, observe=35, rate=40):
    variants = ["naive", "full"]
    runs = []
    for rep in range(reps):
        for variant in rotate(variants, rep):
            say(f"badrelease rep {rep + 1}/{reps} variant {variant}")
            deploy_variant(kube, PATHS[variant])
            lg = start_loadgen(kube, f"bad-{variant}-{rep}", duration, rate)
            time.sleep(trigger_at)
            kube.run("set", "env", "deploy/app", "BAD_RELEASE=1")
            t0 = time.monotonic()
            completed_at = stalled_at = None
            min_available, last = 99, None
            while time.monotonic() - t0 < observe:
                last = _deployment_state(kube)
                min_available = min(min_available, last["available"])
                now = time.monotonic() - t0
                if last["complete"] and completed_at is None:
                    completed_at = now
                if last["deadline_exceeded"] and stalled_at is None:
                    stalled_at = now
                time.sleep(0.5)
            # remediation: roll back to the previous revision
            kube.run("rollout", "undo", "deploy/app")
            t_undo = time.monotonic()
            kube.rollout_status(timeout=120)
            undo_s = time.monotonic() - t_undo
            summary = finish_loadgen(kube, lg, duration)
            run = {"variant": variant, "rep": rep,
                   "rollout_completed_with_bad_pods": completed_at is not None,
                   "seconds_to_complete": completed_at,
                   "deadline_exceeded_detected": stalled_at is not None,
                   "seconds_to_deadline_exceeded": stalled_at,
                   "min_available_replicas": min_available,
                   "state_at_end_of_observation": last, "undo_seconds": undo_s,
                   "bad_responses": summary["errors"].get("http_500", 0), "load": summary}
            runs.append(run)
            record("badrelease", run)
            say(f"  completed={completed_at is not None} stalled={stalled_at is not None} "
                f"min_available={min_available} failed {summary['failed']}/{summary['total']}")
    by_variant = {}
    for v in variants:
        rs = [r for r in runs if r["variant"] == v]
        by_variant[v] = {
            "label": LABELS[v],
            **stats.aggregate([r["load"] for r in rs]),
            "runs_where_bad_rollout_completed": sum(r["rollout_completed_with_bad_pods"] for r in rs),
            "runs_where_deadline_exceeded_detected": sum(r["deadline_exceeded_detected"] for r in rs),
            "seconds_to_deadline_exceeded": stats.summarise_numbers(
                [r["seconds_to_deadline_exceeded"] for r in rs if r["seconds_to_deadline_exceeded"] is not None]),
            "min_available_replicas": min(r["min_available_replicas"] for r in rs),
            "bad_responses": sum(r["bad_responses"] for r in rs),
            "undo_seconds": stats.summarise_numbers([r["undo_seconds"] for r in rs]),
        }
    return {"config": {"reps": reps, "rate_rps": rate, "duration_s": duration, "bad_release_at_s": trigger_at,
                       "observation_window_s": observe, "progressDeadlineSeconds": 20},
            "variants": by_variant}


# ---- experiment 3: node drain and the PodDisruptionBudget -------------------------------------

def exp_drain(kube, reps, duration=70, rate=40):
    variants = ["full", "no-pdb"]
    runs = []
    for rep in range(reps):
        for variant in rotate(variants, rep):
            say(f"drain rep {rep + 1}/{reps} variant {variant}")
            deploy_variant(kube, PATHS[variant])
            per_node = {}
            for p in kube.server_pods():
                per_node[p["spec"]["nodeName"]] = per_node.get(p["spec"]["nodeName"], 0) + 1
            node = max(per_node, key=per_node.get)
            lg = start_loadgen(kube, f"drain-{variant}-{rep}", duration, rate)
            time.sleep(5)
            samples, stop = [], threading.Event()
            t0 = time.monotonic()

            def sample():
                while not stop.is_set():
                    try:
                        samples.append((time.monotonic() - t0, kube.ready_count()))
                    except (KubectlError, subprocess.TimeoutExpired):
                        pass
                    time.sleep(0.2)

            th = threading.Thread(target=sample)
            th.start()
            drain = subprocess.run(
                kube.base_cmd(False) + ["drain", node, "--ignore-daemonsets", "--delete-emptydir-data",
                                        "--timeout=120s"],
                capture_output=True, text=True, encoding="utf-8", timeout=200)
            drain_s = time.monotonic() - t0
            try:
                kube.wait_ready(3, timeout=90)
            except TimeoutError:
                pass
            time.sleep(2)
            stop.set()
            th.join()
            kube.run("uncordon", node, namespaced=False)
            summary = finish_loadgen(kube, lg, duration)
            out = drain.stdout + drain.stderr
            run = {"variant": variant, "rep": rep, "drained_node": node,
                   "pods_on_node_before": per_node[node], "drain_exit_code": drain.returncode,
                   "drain_seconds": drain_s,
                   "min_ready_pods": min(n for _, n in samples),
                   "samples": len(samples),
                   "evictions_blocked_by_pdb": out.count("disruption budget"),
                   "load": summary}
            runs.append(run)
            record("drain", run)
            say(f"  node {node} ({per_node[node]} pods) drain {drain_s:.1f}s exit {drain.returncode} "
                f"min_ready {run['min_ready_pods']} blocked {run['evictions_blocked_by_pdb']} "
                f"failed {summary['failed']}/{summary['total']}")
    by_variant = {}
    for v in variants:
        rs = [r for r in runs if r["variant"] == v]
        by_variant[v] = {
            "label": LABELS[v],
            **stats.aggregate([r["load"] for r in rs]),
            "min_ready_pods_per_run": [r["min_ready_pods"] for r in rs],
            "drain_seconds": stats.summarise_numbers([r["drain_seconds"] for r in rs]),
            "evictions_blocked_by_pdb": [r["evictions_blocked_by_pdb"] for r in rs],
            "pods_on_drained_node": [r["pods_on_node_before"] for r in rs],
        }
    return {"config": {"reps": reps, "replicas": 3, "workers": 2, "pdb_maxUnavailable": 1,
                       "rate_rps": rate, "duration_s": duration}, "variants": by_variant}


# ---- experiment 4: NetworkPolicy ----------------------------------------------------------

NETTEST = ("import http.client\n"
           "try:\n"
           "    c = http.client.HTTPConnection('app', 80, timeout=3)\n"
           "    c.request('GET', '/work')\n"
           "    print(c.getresponse().status)\n"
           "except Exception as e:\n"
           "    print(type(e).__name__)\n")


def exp_netpol(kube, reps):
    deploy_variant(kube, PATHS["full"])
    results = {"client": [], "intruder": []}
    for rep in range(reps):
        for role in ("client", "intruder"):
            name = f"nettest-{role}-{rep}"
            kube.run("delete", "pod", name, "--ignore-not-found", "--wait=true")
            kube.run("apply", "-f", "-", input=json.dumps(client_pod(name, ["-c", NETTEST], role=role)))
            wait_phase(kube, name, {"Succeeded", "Failed"}, timeout=60)
            results[role].append(kube.run("logs", name).stdout.strip())
            kube.run("delete", "pod", name, "--ignore-not-found", "--wait=false")
    enforced = (all(r == "200" for r in results["client"])
                and all(r != "200" for r in results["intruder"]))
    return {"reps": reps, "client_with_label": results["client"], "pod_without_label": results["intruder"],
            "policy_enforced": enforced}


# ---- experiment 5: manifests validate and the prod overlay deploys ------------------------

def exp_validate(kube):
    out = {"server_side_dry_run": {}}
    for name in ("full", "naive", "probes", "no-pdb"):
        proc = kube.apply_k(PATHS[name], "--dry-run=server", check=False)
        out["server_side_dry_run"][name] = proc.returncode == 0
    prod = Kube(namespace="rollout-lab-prod")
    try:
        kube.apply_k(PATHS["prod"])
        prod.run("rollout", "status", "deploy/app", "--timeout=150s", timeout=170)
        hpa = prod.json("get", "hpa", "app")
        dep = prod.deployment()
        pdb = prod.json("get", "pdb", "app")
        out["prod_overlay"] = {
            "deployed": True,
            "replicas_ready": dep["status"].get("readyReplicas"),
            "pdb_min_available": pdb["spec"].get("minAvailable"),
            "hpa_min_max": [hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]],
            "hpa_conditions": [{"type": c["type"], "status": c["status"], "reason": c.get("reason")}
                               for c in hpa["status"].get("conditions", [])],
        }
    except (KubectlError, subprocess.TimeoutExpired) as exc:
        out["prod_overlay"] = {"deployed": False, "error": str(exc)[:300]}
    finally:
        kube.run("delete", "namespace", "rollout-lab-prod", "--ignore-not-found", "--wait=true",
                 namespaced=False, check=False, timeout=180)
    return out


# ---- driver ----------------------------------------------------------------------------------

def load_summary():
    path = RESULTS / "summary.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_summary(summary):
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def run(name, reps=None):
    kube = Kube()
    if not kube.reachable():
        raise SystemExit("cluster not reachable: run `python -m rollout cluster-up` first")
    names = ["validate", "netpol", "rollout", "badrelease", "drain"] if name == "all" else [name]
    summary = load_summary()
    summary["environment"] = {**versions(), "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    fns = {"rollout": exp_rollout, "badrelease": exp_badrelease, "drain": exp_drain,
           "netpol": exp_netpol, "validate": lambda k, r: exp_validate(k)}
    for n in names:
        say(f"=== experiment {n}")
        summary[n] = fns[n](kube, reps or DEFAULT_REPS[n])
        save_summary(summary)
    return {n: summary[n] for n in names}
