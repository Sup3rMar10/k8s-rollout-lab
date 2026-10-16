"""Turn results/summary.json into the chart and the results block in README.md."""
import json
import re

from .cluster import ROOT

MARK_START, MARK_END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
COLORS = {"naive": "#b23a2e", "probes": "#d9922b", "full": "#2f7d4f", "no-pdb": "#b23a2e"}
SHORT = {"naive": "A", "probes": "C", "full": "B", "no-pdb": "B, no PDB"}


def _f(x, digits=1):
    return "n/a" if x is None else f"{x:.{digits}f}"


def chart(summary, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2))

    ax = axes[0]
    roll = summary["rollout"]["variants"]
    for i, v in enumerate(["naive", "probes", "full"]):
        per_run = roll[v]["failed_per_run"]
        ax.bar(i, roll[v]["mean_failed_per_run"], color=COLORS[v], alpha=0.35, width=0.6)
        ax.scatter([i] * len(per_run), per_run, color=COLORS[v], zorder=3, s=22)
        ax.text(i, max(per_run) + 6, f"mean {roll[v]['mean_failed_per_run']:.0f}", ha="center", fontsize=9)
    ax.set_xticks(range(3), ["A\nno probes,\nno preStop", "C\nprobes only", "B\nprobes + preStop\n+ maxUnavailable 0"])
    ax.set_ylabel("failed requests per restart")
    ax.set_title("Rolling restart (each dot is one run)", fontsize=10)

    ax = axes[1]
    bad = summary["badrelease"]["variants"]
    for i, v in enumerate(["naive", "full"]):
        per_run = bad[v]["failed_per_run"]
        ax.bar(i, bad[v]["mean_failed_per_run"], color=COLORS[v], alpha=0.35, width=0.5)
        ax.scatter([i] * len(per_run), per_run, color=COLORS[v], zorder=3, s=22)
        ax.text(i, max(per_run) + 40, f"mean {bad[v]['mean_failed_per_run']:.0f}", ha="center", fontsize=9)
    ax.set_xticks(range(2), ["A\nno readiness probe", "B\nreadiness + maxUnavailable 0"])
    ax.set_ylabel("failed requests while the bad release is live")
    ax.set_title("Bad release (each dot is one run)", fontsize=10)

    ax = axes[2]
    drain = summary["drain"]["variants"]
    for i, v in enumerate(["full", "no-pdb"]):
        runs = drain[v]["min_ready_pods_per_run"]
        ax.scatter([i] * len(runs), runs, color=COLORS[v], zorder=3, s=40)
    ax.axhline(3, color="#888", lw=0.8, ls="--")
    ax.text(1.45, 3.04, "3 replicas", ha="right", fontsize=8, color="#555")
    ax.set_xticks(range(2), ["with PDB\n(maxUnavailable 1)", "without PDB"])
    ax.set_xlim(-0.5, 1.5)
    ax.set_ylim(0, 3.6)
    ax.set_yticks(range(0, 4))
    ax.set_ylabel("lowest ready-pod count during node drain")
    ax.set_title("Node drain (each dot is one run)", fontsize=10)

    for ax in axes:
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def tables(s):
    env, out = s["environment"], []
    out.append("### Setup\n")
    out.append("| | |\n|---|---|")
    out.append(f"| Cluster | kind, 1 control-plane + 2 worker nodes, Kubernetes {env['kubernetes_server']}, "
               f"{env['container_runtime']}, CNI kindnet |")
    out.append(f"| Tools | {env['kind']}, kubectl client {env['kubectl_client']}, Docker {env['docker']}, "
               f"Python {env['python']} |")
    rc, bc, dc = s["rollout"]["config"], s["badrelease"]["config"], s["drain"]["config"]
    out.append(f"| Load | {rc['rate_rps']} requests/s from an in-cluster pod, a new connection per request, "
               f"2 s timeout, through the Service |")
    out.append(f"| App | 3 replicas, {rc['startup_delay_s']} s startup delay, 20 ms of work per request |")
    out.append(f"| Recorded | {env['recorded']} |\n")

    out.append("### 1. Rolling restart under load\n")
    out.append(f"Each run restarts the Deployment ({rc['restart_at_s']} s into a {rc['duration_s']} s load run) "
               f"and counts requests that fail. {rc['reps']} runs per configuration, run order rotated each round.\n")
    out.append("| Configuration | Failed requests per run | Mean | Failure rate | Failure kinds (all runs) | "
               "Runs with zero failures | Kubernetes says the rollout took (s) |")
    out.append("|---|---|---|---|---|---|---|")
    for v in ("naive", "probes", "full"):
        r = s["rollout"]["variants"][v]
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(r["errors"].items())) or "none"
        out.append(f"| {r['label']} | {', '.join(str(x) for x in r['failed_per_run'])} | "
                   f"{r['mean_failed_per_run']:.1f} | {100 * r['failure_rate']:.2f}% | {kinds} | "
                   f"{r['runs_with_zero_failures']} of {r['runs']} | {_f(r['rollout_seconds']['mean'])} |")
    out.append("")

    out.append("### 2. A bad release\n")
    out.append(f"{bc['reps']} runs per configuration. {bc['bad_release_at_s']} s into the run the Deployment is "
               f"changed to a release whose `/readyz` answers 503 and whose `/work` answers 500 "
               f"(`BAD_RELEASE=1`). The rollout is watched for {bc['observation_window_s']} s "
               f"(`progressDeadlineSeconds: {bc['progressDeadlineSeconds']}`), then `kubectl rollout undo`.\n")
    out.append("| Configuration | Rollout finished with bad pods serving | Deadline exceeded detected | "
               "Seconds to detect (mean) | Lowest available replicas | Failed requests per run | 500 responses |")
    out.append("|---|---|---|---|---|---|---|")
    for v in ("naive", "full"):
        r = s["badrelease"]["variants"][v]
        out.append(f"| {r['label']} | {r['runs_where_bad_rollout_completed']} of {r['runs']} | "
                   f"{r['runs_where_deadline_exceeded_detected']} of {r['runs']} | "
                   f"{_f(r['seconds_to_deadline_exceeded'].get('mean'))} | {r['min_available_replicas']} | "
                   f"{', '.join(str(x) for x in r['failed_per_run'])} | {r['bad_responses']} |")
    out.append("")

    out.append("### 3. Draining a node\n")
    out.append(f"{dc['reps']} runs per configuration. The worker holding the most app pods is drained "
               f"(`kubectl drain`) under load. The PDB allows one pod to be unavailable.\n")
    out.append("| Configuration | Pods on the drained node | Lowest ready pods during the drain | "
               "Evictions refused by the PDB | Drain took (s, mean) | Failed requests per run |")
    out.append("|---|---|---|---|---|---|")
    for v in ("full", "no-pdb"):
        r = s["drain"]["variants"][v]
        out.append(f"| {r['label']} | {', '.join(str(x) for x in r['pods_on_drained_node'])} | "
                   f"{', '.join(str(x) for x in r['min_ready_pods_per_run'])} | "
                   f"{', '.join(str(x) for x in r['evictions_blocked_by_pdb'])} | "
                   f"{_f(r['drain_seconds']['mean'])} | {', '.join(str(x) for x in r['failed_per_run'])} |")
    out.append("")

    n, v = s["netpol"], s["validate"]
    out.append("### 4. Checks that are not timing experiments\n")
    out.append(f"- **NetworkPolicy enforced: {'yes' if n['policy_enforced'] else 'NO'}.** A pod labelled "
               f"`role=client` got {', '.join(n['client_with_label'])} from the app; a pod without the label got "
               f"{', '.join(n['pod_without_label'])} ({n['reps']} tries each, same node). kindnet enforces policy, and "
               f"kubelet probes still pass under default deny.")
    ok = [k for k, val in v["server_side_dry_run"].items() if val]
    out.append(f"- **Server-side dry run** accepted: {', '.join(ok)}.")
    p = v.get("prod_overlay", {})
    if p.get("deployed"):
        hpa = ", ".join(f"{c['type']}={c['status']}" + (f" ({c['reason']})" if c.get("reason") else "")
                        for c in p["hpa_conditions"])
        out.append(f"- **Prod overlay deployed for real:** {p['replicas_ready']} replicas ready, "
                   f"PDB minAvailable {p['pdb_min_available']}, HPA {p['hpa_min_max'][0]} to {p['hpa_min_max'][1]}. "
                   f"HPA conditions: {hpa}. There is no metrics-server in kind, so autoscaling itself was not exercised.")
    return "\n".join(out)


def build():
    summary = json.loads((ROOT / "results" / "summary.json").read_text(encoding="utf-8"))
    chart(summary, ROOT / "results" / "rollout_results.png")
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    block = (f"{MARK_START}\n\n![Rollout, bad release and drain results](results/rollout_results.png)\n\n"
             f"{tables(summary)}\n\n{MARK_END}")
    if MARK_START in text:
        text = re.sub(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), lambda m: block, text, flags=re.S)
        readme.write_text(text, encoding="utf-8", newline="\n")
        print("README results block and results/rollout_results.png rebuilt")
    else:
        print("README has no results markers; chart rebuilt only")
