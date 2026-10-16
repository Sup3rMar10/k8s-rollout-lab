"""Turn results/summary.json into the chart and the results block in README.md."""
import json
import re

from .cluster import ROOT

MARK_START, MARK_END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
COLORS = {"naive": "#b23a2e", "probes": "#d9922b", "full": "#2f7d4f", "no-pdb": "#b23a2e"}
SHORT = {"naive": "A", "probes": "C", "full": "B", "no-pdb": "B, no PDB"}


def _f(x, digits=1):
    return "n/a" if x is None else f"{x:.{digits}f}"


def _m(x):
    return f"{x:.1f}" if x < 10 else f"{x:.0f}"


def chart(summary, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(9.5, 4.2))

    ax = axes[0]
    roll = summary["rollout"]["variants"]
    for i, v in enumerate(["naive", "probes", "full"]):
        per_run = roll[v]["failed_per_run"]
        ax.bar(i, roll[v]["mean_failed_per_run"], color=COLORS[v], alpha=0.35, width=0.6)
        ax.scatter([i] * len(per_run), per_run, color=COLORS[v], zorder=3, s=22)
        ax.text(i, max(per_run) + 6, f"mean {_m(roll[v]['mean_failed_per_run'])}", ha="center", fontsize=9)
    ax.set_xticks(range(3), ["A\nno probes,\nno preStop", "C\nprobes only", "B\nprobes + preStop\n+ maxUnavailable 0"])
    ax.set_ylabel("failed requests per restart")
    ax.set_title("Rolling restart (each dot is one run)", fontsize=10)

    ax = axes[1]
    bad = summary["badrelease"]["variants"]
    for i, v in enumerate(["naive", "full"]):
        per_run = bad[v]["failed_per_run"]
        ax.bar(i, bad[v]["mean_failed_per_run"], color=COLORS[v], alpha=0.35, width=0.5)
        ax.scatter([i] * len(per_run), per_run, color=COLORS[v], zorder=3, s=22)
        ax.text(i, max(per_run) + 40, f"mean {_m(bad[v]['mean_failed_per_run'])}", ha="center", fontsize=9)
    ax.set_xticks(range(2), ["A\nno readiness probe", "B\nreadiness + maxUnavailable 0"])
    ax.set_ylabel("failed requests while the bad release is live")
    ax.set_title("Bad release (each dot is one run)", fontsize=10)

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
    def shape(name):
        nodes = s[name]["cluster_nodes"]
        w = sum(1 for n in nodes if "worker" in n)
        return f"1 control-plane + {w} worker{'s' if w != 1 else ''}"

    out.append(f"| Cluster | kind, Kubernetes {env['kubernetes_server']}, {env['container_runtime']}, CNI kindnet. "
               f"Rolling restart ran on {shape('rollout')}; bad release, NetworkPolicy and prod-overlay checks "
               f"ran on {shape('badrelease')} |")
    out.append(f"| Tools | {env['kind']}, kubectl client {env['kubectl_client']}, Docker {env['docker']}, "
               f"Python {env['python']} |")
    rc, bc = s["rollout"]["config"], s["badrelease"]["config"]
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

    n, v = s["netpol"], s["validate"]
    out.append("### 3. Checks that are not timing experiments\n")
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
    block = (f"{MARK_START}\n\n![Rolling restart and bad-release results](results/rollout_results.png)\n\n"
             f"{tables(summary)}\n\n{MARK_END}")
    if MARK_START in text:
        text = re.sub(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), lambda m: block, text, flags=re.S)
        readme.write_text(text, encoding="utf-8", newline="\n")
        print("README results block and results/rollout_results.png rebuilt")
    else:
        print("README has no results markers; chart rebuilt only")
