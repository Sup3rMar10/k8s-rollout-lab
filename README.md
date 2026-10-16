# ☸️ Kubernetes Rollout Lab

A small service, deployed with Kustomize, and three experiments that measure what Kubernetes does to the people using it while you change things: a rolling restart, a bad release, and a node drain. Every claim in the manifests ("this config gives zero-downtime deploys") is tested by sending steady traffic through the change and counting the requests that fail.

It runs on a laptop, in a local [kind](https://kind.sigs.k8s.io/) cluster. That constraint is stated again in the limits below, because it shapes how far the numbers can be trusted.

This is original code. The app, the load generator, the manifests and the experiment runner are all written for this repo.

## What is in it

```
 python -m rollout ── kubectl ──▶  kind cluster: 1 control-plane + 2 workers, namespace rollout-lab
        │
        │ starts                    NetworkPolicy: default deny, then allow role=client → :8080
        ▼                         ┌──────────────────────────────────────────────────────────┐
 load-generator pod ── HTTP ───▶  │ Service "app" ──▶ Deployment "app" (3 pods)              │
 (role=client, on the             │                    startup / readiness / liveness probes  │
  control-plane node)             │                    preStop sleep, terminationGracePeriod  │
        ▲                         │                    non-root, read-only rootfs, drop ALL   │
        │                         │                    PodDisruptionBudget (maxUnavailable 1) │
        └─ JSON summary ◀─────────│                    HPA (prod overlay only)                │
                                  └──────────────────────────────────────────────────────────┘
```

- **`app/server.py`**: a standard-library HTTP service with `/healthz`, `/readyz` and `/work`. It has a configurable startup delay, shuts down on SIGTERM by finishing the requests in flight, and can simulate a bad release (`BAD_RELEASE=1`: readiness fails and `/work` returns 500). Every configuration runs this same image and code, so a failed request comes from the manifest, not from the app.
- **`deploy/`**: Kustomize only (the one built into `kubectl`, no Helm). `base` is the full configuration. `overlays/dev` is what the experiments run. `overlays/prod` changes the namespace, replicas, resources, PDB and adds an HPA. `variants/` are deliberately weaker copies used as baselines (no probes and no preStop, probes only, no PDB).
- **`rollout/loadgen.py`**: a fixed-rate load generator that opens a new connection for every request, so each one is routed afresh by the Service. It runs inside the cluster, as a pod, so the NetworkPolicy applies to it like any other client.
- **`rollout/experiments.py`**: the experiments. Each drives `kubectl`, runs the load generator through the change, and appends every run to `results/runs.jsonl`.

## How the comparison is kept fair

- **Same image, same app, same load.** Only the manifest differs between configurations.
- **Config A is a deliberate baseline, not a claim about what everyone runs.** It is a Deployment with no readiness probe, no preStop hook and Kubernetes' default rolling update. Config C adds the probes only, to show how much of the gain comes from readiness gating alone. Config B is the full configuration.
- **Repetitions, and the order rotates.** Every configuration is run several times, and the order is rotated each round so a slow drift in the machine does not always land on the same configuration.
- **Clean slate each run.** The Deployment and PDB are deleted and re-applied before every run.
- **Counts per run are the result.** Requests in one failure burst are not independent, so there is no confidence interval on a pooled rate. The table lists the count from every run, so the spread is visible.
- **Every run is on disk.** `results/runs.jsonl` has one line per run, including the per-second failure counts.

## Results

<!-- RESULTS:START -->
<!-- RESULTS:END -->

## What the results say

RESULTS_ANALYSIS_PLACEHOLDER

## Run it

Needs Docker, `kubectl`, [`kind`](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) and Python 3.10+.

```bash
git clone https://github.com/Sup3rMar10/k8s-rollout-lab.git
cd k8s-rollout-lab
pip install -r requirements.txt

pytest -q                                   # lint, unit and app tests; no cluster needed
python -m rollout cluster-up                # kind cluster (1 control-plane, 2 workers), image built and loaded
python -m rollout experiment netpol         # about 1 minute
python -m rollout experiment rollout        # about 20 minutes (5 runs x 3 configurations)
python -m rollout experiment badrelease     # about 10 minutes
python -m rollout experiment drain          # about 10 minutes
python -m rollout report                    # rebuilds the chart and the results block above
python -m rollout cluster-down              # deletes the cluster
```

On Windows with Git Bash, set `MSYS_NO_PATHCONV=1` so Docker arguments such as `/tmp` are not rewritten. If `kind` is not on `PATH`, set `KIND` to its location. The runner always passes `--context kind-rollout-lab`, so it never touches another cluster in your kubeconfig.

`ROLLOUT_E2E=1 pytest tests/test_experiments.py` also runs a smoke test against the running cluster. It is opt-in because it redeploys the app.

## Limits

- **kind on a laptop is not production.** All nodes are containers sharing one Docker VM and one CPU budget. There is no real network, no cloud load balancer and no cross-zone traffic, and timing is noisy. Run-to-run spread is in the tables.
- **The load comes from inside the cluster.** It goes through the Service, not through an Ingress or a cloud load balancer. A real load balancer has its own deregistration delay, which is the number a preStop sleep has to cover. Here the sleep is 5 s, picked to cover kube-proxy and endpoint propagation on this cluster, not tuned for anything else.
- **The app is tiny and well behaved on SIGTERM.** A real service that ignores SIGTERM, or holds long-lived connections, behaves differently.
- **The bad release is simulated** with an environment variable, and it is a very obvious failure (readiness never passes). Subtler regressions, such as a release that is ready but slow or wrong, are not caught by readiness probes and are not covered.
- **The HPA is only checked for being accepted and deployed.** There is no metrics-server in kind, so it never scales and its condition says so. Autoscaling behaviour under load is not measured.
- **Small samples.** 3 to 5 runs per configuration. The gaps between configurations are large relative to the spread, but this is a demonstration, not a benchmark.
- **Not covered:** multiple availability zones, cluster autoscaling, a service mesh, StatefulSets, pod security admission, and anything that needs a real cloud.
- **Kubernetes version.** Everything was run against the Kubernetes version recorded in the results table. The native `preStop` `sleep` action needs a recent cluster.

## What I would do at a customer

1. **Audit before measuring.** Run the same lint rules (`tests/test_manifests.py`) over their Deployments: requests and limits, probes, PDBs, non-root, pinned image tags. Most findings are free to fix.
2. **Measure their own restart.** Point the load generator at their staging service through the same path real traffic takes (their ingress or load balancer), restart one Deployment, and count failures. That gives a baseline in their own units before anything is changed.
3. **Find the real deregistration delay.** Read their load balancer's settings and size the preStop sleep and grace period from that, not from a default.
4. **Change one thing at a time**, re-measure, and keep the numbers in the pull request.
5. **Make it stick.** Turn the lint rules into an admission policy or a CI check so a new service cannot ship without probes and a PDB.

## Layout

```
app/                server.py (the service) and Dockerfile
deploy/base         Deployment, Service, PDB, NetworkPolicy, Namespace
deploy/overlays     dev (used by the experiments) and prod (HPA, bigger, own namespace)
deploy/variants     naive, probes, no-pdb: deliberately weaker baselines
rollout/            loadgen, kubectl wrapper, cluster setup, experiments, report, CLI
tests/              manifest lint, load-generator maths, app behaviour, helpers
results/            summary.json, runs.jsonl (every run), rollout_results.png
kind-config.yaml    1 control-plane + 2 workers
```

## License

MIT. See [LICENSE](LICENSE).
