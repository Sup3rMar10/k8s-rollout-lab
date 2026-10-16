"""Helper logic that needs no cluster, plus an end-to-end smoke test that is skipped
unless the lab's kind cluster is running."""
import json
import os

import pytest

from rollout import experiments, kube


def test_rotate_cycles_the_run_order_without_losing_items():
    items = ["a", "b", "c"]
    assert experiments.rotate(items, 0) == ["a", "b", "c"]
    assert experiments.rotate(items, 1) == ["b", "c", "a"]
    assert experiments.rotate(items, 4) == ["b", "c", "a"]
    assert all(sorted(experiments.rotate(items, k)) == items for k in range(6))


def test_client_pod_is_hardened_and_labelled_for_the_network_policy():
    pod = experiments.client_pod("x", ["-c", "print(1)"])
    assert pod["metadata"]["labels"]["role"] == "client"
    c = pod["spec"]["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
    assert pod["spec"]["restartPolicy"] == "Never"
    assert pod["spec"]["nodeSelector"] == {"node-role.kubernetes.io/control-plane": ""}
    json.dumps(pod)  # must serialise: it is piped to kubectl as JSON


def test_intruder_pod_does_not_carry_the_client_label():
    assert experiments.client_pod("y", [], role="intruder")["metadata"]["labels"]["role"] == "intruder"


def test_pod_ready_ignores_terminating_pods():
    ready = {"metadata": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    terminating = {"metadata": {"deletionTimestamp": "2026-01-01T00:00:00Z"},
                   "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    not_ready = {"metadata": {}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}}
    assert kube.Kube.pod_ready(ready) is True
    assert kube.Kube.pod_ready(terminating) is False
    assert kube.Kube.pod_ready(not_ready) is False


def _cluster_up():
    try:
        return kube.Kube().reachable()
    except FileNotFoundError:
        return False


# Opt-in (ROLLOUT_E2E=1): it redeploys the app, so it must never run while an experiment is running.
@pytest.mark.skipif(os.environ.get("ROLLOUT_E2E") != "1" or not _cluster_up(),
                    reason="needs ROLLOUT_E2E=1 and the kind cluster rollout-lab running")
def test_cluster_smoke_full_config_deploys_with_pdb_and_policies():
    k = kube.Kube()
    kube.deploy_variant(k, experiments.PATHS["full"])
    assert k.ready_count() == 3
    assert k.json("get", "pdb", "app")["spec"]["maxUnavailable"] == 1
    names = {p["metadata"]["name"] for p in k.json("get", "networkpolicy")["items"]}
    assert {"default-deny-ingress", "allow-clients-to-app"} <= names
