# ☸️ Kubernetes Rollout Lab

A small service, deployed with Kustomize, and two experiments that measure what Kubernetes does to the people using it while you change things: a rolling restart and a bad release. Every claim in the manifests ("this config gives zero-downtime deploys") is tested by sending steady traffic through the change and counting the requests that fail. There is also a NetworkPolicy check and a check that the production overlay deploys.

**One thing was not measured: node drain with the PodDisruptionBudget.** The manifest and its lint test are here, but the drain experiment was not run for the published results, so what the PDB does during a drain is untested in this repo. See [Limits](#limits) for why.

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
- **`deploy/`**: Kustomize only (the one built into `kubectl`, no Helm). `base` is the full configuration. `overlays/dev` is what the experiments run. `overlays/prod` changes the namespace, replicas, resources, PDB and adds an HPA. `variants/` are deliberately weaker copies used as baselines (no probes and no preStop, probes only; `no-pdb` was made for the drain experiment that was not run).
- **`rollout/loadgen.py`**: a fixed-rate load generator that opens a new connection for every request, so each one is routed afresh by the Service. It runs inside the cluster, as a pod, so the NetworkPolicy applies to it like any other client.
- **`rollout/experiments.py`**: the experiments. Each drives `kubectl`, runs the load generator through the change, and appends every run to `results/runs.jsonl`. The drain experiment is written but opt-in (`experiment drain`) and was not run for the results.

## How the comparison is kept fair

- **Same image, same app, same load.** Only the manifest differs between configurations.
- **Config A is a deliberate baseline, not a claim about what everyone runs.** It is a Deployment with no readiness probe, no preStop hook and Kubernetes' default rolling update. Config C adds the probes only, to show how much of the gain comes from readiness gating alone. Config B is the full configuration.
- **Repetitions, and the order rotates.** Every configuration is run several times, and the order is rotated each round so a slow drift in the machine does not always land on the same configuration.
- **Clean slate each run.** The Deployment and PDB are deleted and re-applied before every run.
- **Counts per run are the result.** Requests in one failure burst are not independent, so there is no confidence interval on a pooled rate. The table lists the count from every run, so the spread is visible.
- **Every run is on disk.** `results/runs.jsonl` has one line per run, including the per-second failure counts.

## Results

<!-- RESULTS:START -->

![Rolling restart and bad-release results](results/rollout_results.png)

### Setup

| | |
|---|---|
| Cluster | kind, Kubernetes v1.37.0, containerd://2.3.4, CNI kindnet. Rolling restart ran on 1 control-plane + 1 worker; bad release, NetworkPolicy and prod-overlay checks ran on 1 control-plane + 2 workers |
| Tools | kind v0.33.0 go1.26.7 windows/amd64, kubectl client v1.36.1, Docker 29.8.2, Python 3.13.13 |
| Load | 40 requests/s from an in-cluster pod, a new connection per request, 2 s timeout, through the Service |
| App | 3 replicas, 3 s startup delay, 20 ms of work per request |
| Recorded | 2026-10-16T14:56:10+00:00 |

### 1. Rolling restart under load

Each run restarts the Deployment (6 s into a 50 s load run) and counts requests that fail. 5 runs per configuration, run order rotated each round.

| Configuration | Failed requests per run | Mean | Failure rate | Failure kinds (all runs) | Runs with zero failures | Kubernetes says the rollout took (s) |
|---|---|---|---|---|---|---|
| A: no probes, no preStop, default rolling update | 183, 169, 165, 171, 163 | 170.2 | 8.51% | refused 779, reset 13, timeout 59 | 0 of 5 | 1.3 |
| C: readiness + startup probes only | 20, 37, 22, 28, 32 | 27.8 | 1.39% | refused 11, reset 15, timeout 113 | 0 of 5 | 15.3 |
| B: probes + preStop + maxUnavailable 0 | 0, 0, 0, 0, 0 | 0.0 | 0.00% | none | 5 of 5 | 15.3 |

### 2. A bad release

3 runs per configuration. 6 s into the run the Deployment is changed to a release whose `/readyz` answers 503 and whose `/work` answers 500 (`BAD_RELEASE=1`). The rollout is watched for 35 s (`progressDeadlineSeconds: 20`), then `kubectl rollout undo`.

| Configuration | Rollout finished with bad pods serving | Deadline exceeded detected | Seconds to detect (mean) | Lowest available replicas | Failed requests per run | 500 responses |
|---|---|---|---|---|---|---|
| A: no probes, no preStop, default rolling update | 3 of 3 | 0 of 3 | n/a | 3 | 1665, 1631, 1591 | 3475 |
| B: probes + preStop + maxUnavailable 0 | 0 of 3 | 3 of 3 | 20.9 | 3 | 0, 1, 0 | 0 |

### 3. Checks that are not timing experiments

- **NetworkPolicy enforced: yes.** A pod labelled `role=client` got 200, 200, 200 from the app; a pod without the label got TimeoutError, TimeoutError, TimeoutError (3 tries each, same node). kindnet enforces policy, and kubelet probes still pass under default deny.
- **Server-side dry run** accepted: full, naive, probes, no-pdb.
- **Prod overlay deployed for real:** 4 replicas ready, PDB minAvailable 3, HPA 4 to 12. HPA conditions: AbleToScale=True (SucceededGetScale), ScalingActive=False (FailedGetResourceMetric). There is no metrics-server in kind, so autoscaling itself was not exercised.

<!-- RESULTS:END -->

## What the results say

All counts below are from `results/runs.jsonl` (one line per run) and `results/summary.json`.

### Rolling restart (5 runs per configuration, 10,004 requests each)

- **Config A lost 8.5% of its requests** (163 to 183 per run, 851 in total) and **config C lost 1.4%** (20 to 37 per run, 139 in total). **Config B lost none, in all 5 runs** (0 of 10,004). The three ranges do not overlap, and the spread within each is small next to the gaps.
- **A fails because the pod is "ready" before the app is.** With no readiness probe a new pod joins the Service the moment its container starts, 3 s before the app opens its port. 779 of A's 851 failures were refused connections, and the per-second counts show 78 to 100% of requests failing in each of seconds 7 to 10 of the run, in every run. Kubernetes reported the rollout finished in 1.2 to 1.5 s while the service was mostly down, which is why "rollout succeeded" is not a health signal.
- **C fixes that and still leaks.** 113 of C's 139 failures were timeouts. They come in three short bursts, at about 11 to 12 s, 16 to 17 s and 21 to 22 s into the run: one per old pod being removed. That fits requests still being routed to a pod that has started shutting down, which is what the preStop sleep exists to cover, but this experiment did not isolate that cause.
- **B's gain cannot be split between its two changes.** B adds both the preStop sleep and `maxUnavailable: 0` to C. They were not tested separately, so the data say B works and C does not, not which of the two matters more.
- **Some isolated stragglers were not investigated.** C had 1 to 3 failures in a single second at 25 to 45 s into 4 of its 5 runs, and A had one such second (32 s) in one run. They are included in the counts above.
- Latency of successful requests was the same everywhere: the 99th percentile was 26 ms in all 15 runs. The cost of a bad rollout here is failed requests, not slow ones.

### Bad release (3 runs per configuration)

- **Without a readiness probe (A), all 3 bad rollouts "completed".** Every pod was replaced by a broken one, `ProgressDeadlineExceeded` never fired (0 of 3), and 1,591 to 1,665 requests failed per run: 3,475 HTTP 500s in total, the rest refused connections (1,301), resets (16) and timeouts (95) as pods churned. Nothing in Kubernetes flagged it. Someone had to notice and run `kubectl rollout undo`.
- **With readiness gating and `maxUnavailable: 0` (B), the bad rollout stalled and traffic was unaffected.** The new pod never became ready, the old pods kept serving, and available replicas never dropped below 3. The deadline was exceeded at 20.5 to 21.4 s (mean 20.9 s, against `progressDeadlineSeconds: 20`) in 3 of 3 runs, which is the signal a pipeline or alert can act on. Rolling back took 0.1 to 0.4 s because only the stuck pod had to go.
- **B had one failed request in 3 runs** (one connection reset late in one run, after the observation window had ended). It is in the table and was not investigated.
- The bad release is an environment variable that fails readiness. A release that passes readiness and is wrong or slow is not caught by any of this.

### The other checks

The NetworkPolicy blocked an unlabelled pod and admitted a `role=client` pod in 3 of 3 tries each, and kubelet probes kept passing under default deny. The prod overlay deployed for real with its PDB and HPA objects. The HPA never scaled, because kind has no metrics-server; its condition says so.

### What was not measured

**Node drain with the PodDisruptionBudget was not run.** The PDB manifest (`maxUnavailable: 1`, selecting the app pods) and a lint test for it are in the repo, and a `drain` experiment is written (`python -m rollout experiment drain`), but no drain result is reported here or anywhere in this repo. What the PDB does during a drain is untested. The reason is laptop memory: the drain needs the 3-node cluster, and the machine ran out of memory twice (below).

## How these numbers were produced, and what went wrong

- **Different cluster shapes.** The rolling-restart runs used a smaller cluster, 1 control-plane + 1 worker (`kind-config-small.yaml`), so all three replicas shared one worker. The bad-release, NetworkPolicy and prod-overlay checks ran earlier on 1 control-plane + 2 workers. The setup table says which. Spreading pods over two workers could change the rolling-restart numbers, and I did not test that.
- **Two memory interruptions.** The first long run of all experiments was stopped by the system's low-memory protection after the bad-release runs had finished and one of the six drain runs had been recorded. A second attempt was abandoned right after the 3-node cluster was created, because free RAM fell to about 0.5 GB. After that the cluster was shrunk, the drain experiment was dropped, and the rolling-restart runs were done alone. The single partial drain record was deleted so no partial data survives.
- **A hung `kubectl apply -k` and the retry.** In the first long run, one `kubectl apply -k` hung for the 120 s timeout and aborted the rolling-restart experiment at its first run. I changed `apply_k` to time out at 60 s and retry once. It does not change anything that is measured. The retry did not fire during the final rolling-restart runs (the log has no retry message). The bad-release runs were recorded before that change.
- **Discarded pilot runs.** One-repetition pilots of the rolling-restart, bad-release and drain experiments were run first to find bugs. Their results were deleted before the real runs and are not used or reported anywhere. No setting was changed because of them.
- **A bug fixed after seeing results.** The `rollout_covered_by_load` flag (did the load run last through the whole rollout?) was computed after waiting for the load generator to finish, so it was recorded as false in every run. I fixed the code and recomputed the flag in `runs.jsonl` from the recorded rollout time (6 s plus at most 15.4 s, inside the 50 s run). No failure count was affected.
- **Timings are noisy.** This is a laptop, all nodes share one Docker VM, and the 15 rolling-restart runs took about 15 minutes with the run order rotated each round. The tables show every run, so judge the spread yourself.
- **The HPA is not exercised.** It is deployed in the prod overlay and its object is validated, but with no metrics-server it never reads a metric and never scales.


## Run it

Needs Docker, `kubectl`, [`kind`](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) and Python 3.10+.

```bash
git clone https://github.com/Sup3rMar10/k8s-rollout-lab.git
cd k8s-rollout-lab
pip install -r requirements.txt

pytest -q                                   # lint, unit and app tests; no cluster needed
python -m rollout cluster-up                # kind cluster (1 control-plane, 2 workers), image built and loaded
                                            # add --small for 1 control-plane + 1 worker if memory is tight
python -m rollout experiment netpol         # about 1 minute
python -m rollout experiment rollout        # about 17 minutes (5 runs x 3 configurations)
python -m rollout experiment badrelease     # about 10 minutes
python -m rollout experiment validate       # about 2 minutes
python -m rollout report                    # rebuilds the chart and the results block above
python -m rollout cluster-down              # deletes the cluster

# not run for the published results; needs the 3-node cluster (about 10 minutes)
python -m rollout experiment drain
```

On Windows with Git Bash, set `MSYS_NO_PATHCONV=1` so Docker arguments such as `/tmp` are not rewritten. If `kind` is not on `PATH`, set `KIND` to its location. The runner always passes `--context kind-rollout-lab`, so it never touches another cluster in your kubeconfig.

`ROLLOUT_E2E=1 pytest tests/test_experiments.py` also runs a smoke test against the running cluster. It is opt-in because it redeploys the app.

## Limits

- **Node drain and the PDB are untested here** (see above). Do not read the PDB manifest as evidence that it works.
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
deploy/variants     naive, probes: deliberately weaker baselines; no-pdb for the drain experiment that was not run
rollout/            loadgen, kubectl wrapper, cluster setup, experiments, report, CLI
tests/              manifest lint, load-generator maths, app behaviour, helpers
results/            summary.json, runs.jsonl (every run), rollout_results.png (no drain data)
kind-config.yaml    1 control-plane + 2 workers (3 nodes)
kind-config-small.yaml  1 control-plane + 1 worker (used for the rolling-restart runs)
```

## License

MIT. See [LICENSE](LICENSE).
