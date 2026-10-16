"""Manifest linting in pure Python. Rules that would otherwise be review comments.

The base files are linted directly. When kubectl is installed the Kustomize overlays are
also rendered and checked, including the variants that deliberately drop protections."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
BASE = DEPLOY / "base"
RENDERED = ["overlays/dev", "overlays/prod", "variants/naive", "variants/probes", "variants/no-pdb"]
needs_kubectl = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")


def load(path):
    with open(path, encoding="utf-8") as f:
        return [d for d in yaml.safe_load_all(f) if d]


def base_docs():
    docs = []
    for p in sorted(BASE.glob("*.yaml")):
        if p.name != "kustomization.yaml":
            docs += load(p)
    return docs


def of_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def render(path):
    out = subprocess.run(["kubectl", "kustomize", str(DEPLOY / path)], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def containers(deployment):
    return deployment["spec"]["template"]["spec"]["containers"]


def check_container_basics(c):
    assert c["resources"]["requests"]["cpu"] and c["resources"]["requests"]["memory"]
    assert c["resources"]["limits"]["cpu"] and c["resources"]["limits"]["memory"]
    image = c["image"]
    assert ":" in image and not image.endswith(":latest"), f"image must carry an explicit non-latest tag: {image}"
    sc = c["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert "ALL" in sc["capabilities"]["drop"]


# ---- the base ----------------------------------------------------------------------------

def test_base_documents_parse_and_have_required_fields():
    docs = base_docs()
    assert {d["kind"] for d in docs} >= {"Namespace", "Deployment", "Service", "PodDisruptionBudget",
                                         "NetworkPolicy"}
    for d in docs:
        assert d["apiVersion"] and d["metadata"]["name"]


def test_every_container_has_limits_requests_tag_and_hardened_context():
    for dep in of_kind(base_docs(), "Deployment"):
        for c in containers(dep):
            check_container_basics(c)


def test_pod_runs_as_non_root_with_default_seccomp():
    pod = of_kind(base_docs(), "Deployment")[0]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] not in (0, None)
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["automountServiceAccountToken"] is False


def test_base_container_has_all_three_probes_and_a_prestop():
    c = containers(of_kind(base_docs(), "Deployment")[0])[0]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert probe in c, f"{probe} missing"
        assert c[probe]["httpGet"]["path"].startswith("/")
    assert c["lifecycle"]["preStop"]["sleep"]["seconds"] > 0


def test_prestop_fits_inside_the_termination_grace_period():
    dep = of_kind(base_docs(), "Deployment")[0]
    sleep = containers(dep)[0]["lifecycle"]["preStop"]["sleep"]["seconds"]
    assert dep["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] > sleep


def test_rolling_update_never_removes_a_pod_before_its_replacement_is_ready():
    strat = of_kind(base_docs(), "Deployment")[0]["spec"]["strategy"]
    assert strat["type"] == "RollingUpdate"
    assert strat["rollingUpdate"]["maxUnavailable"] == 0
    assert strat["rollingUpdate"]["maxSurge"] >= 1


def test_pdb_exists_and_selects_the_deployment_pods():
    docs = base_docs()
    dep = of_kind(docs, "Deployment")[0]
    pdbs = of_kind(docs, "PodDisruptionBudget")
    assert len(pdbs) == 1
    labels = dep["spec"]["template"]["metadata"]["labels"]
    selector = pdbs[0]["spec"]["selector"]["matchLabels"]
    assert all(labels.get(k) == v for k, v in selector.items())
    assert ("minAvailable" in pdbs[0]["spec"]) != ("maxUnavailable" in pdbs[0]["spec"])


def test_service_selector_matches_pod_labels_and_port_name():
    docs = base_docs()
    dep, svc = of_kind(docs, "Deployment")[0], of_kind(docs, "Service")[0]
    labels = dep["spec"]["template"]["metadata"]["labels"]
    assert all(labels.get(k) == v for k, v in svc["spec"]["selector"].items())
    port_names = {p["name"] for p in containers(dep)[0]["ports"]}
    assert svc["spec"]["ports"][0]["targetPort"] in port_names


def test_deployment_selector_matches_its_own_pod_template():
    dep = of_kind(base_docs(), "Deployment")[0]
    labels = dep["spec"]["template"]["metadata"]["labels"]
    assert all(labels.get(k) == v for k, v in dep["spec"]["selector"]["matchLabels"].items())


def test_network_policy_is_default_deny_with_one_scoped_allow():
    pols = of_kind(base_docs(), "NetworkPolicy")
    deny = [p for p in pols if p["spec"]["podSelector"] == {} and not p["spec"].get("ingress")]
    assert len(deny) == 1 and "Ingress" in deny[0]["spec"]["policyTypes"]
    allow = [p for p in pols if p["spec"].get("ingress")]
    assert len(allow) == 1
    rule = allow[0]["spec"]["ingress"][0]
    assert rule["from"][0]["podSelector"]["matchLabels"] == {"role": "client"}
    assert rule["ports"][0]["port"] == 8080
    assert not any("ipBlock" in f for r in allow[0]["spec"]["ingress"] for f in r["from"])


def test_hpa_targets_the_deployment_and_has_sane_bounds():
    hpa = load(DEPLOY / "overlays" / "prod" / "hpa.yaml")[0]
    assert hpa["spec"]["scaleTargetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": "app"}
    assert 1 <= hpa["spec"]["minReplicas"] < hpa["spec"]["maxReplicas"]


def test_dockerfile_drops_root_and_pins_a_tag():
    lines = (ROOT / "app" / "Dockerfile").read_text(encoding="utf-8").splitlines()
    froms = [ln.split()[1] for ln in lines if ln.startswith("FROM ")]
    assert froms and all(":" in f and not f.endswith(":latest") for f in froms)
    users = [ln.split()[1] for ln in lines if ln.startswith("USER ")]
    assert users and users[-1] not in ("root", "0")


def test_kind_config_has_two_workers_so_a_drain_has_somewhere_to_go():
    cfg = load(ROOT / "kind-config.yaml")[0]
    roles = [n["role"] for n in cfg["nodes"]]
    assert roles.count("worker") >= 2 and roles.count("control-plane") == 1


# ---- rendered overlays and variants --------------------------------------------------------

@needs_kubectl
@pytest.mark.parametrize("path", RENDERED)
def test_rendered_variants_keep_resources_security_and_pinned_image(path):
    docs = render(path)
    for dep in of_kind(docs, "Deployment"):
        for c in containers(dep):
            check_container_basics(c)


@needs_kubectl
@pytest.mark.parametrize("path", ["overlays/dev", "overlays/prod", "variants/probes", "variants/no-pdb"])
def test_rendered_configs_with_probes(path):
    c = containers(of_kind(render(path), "Deployment")[0])[0]
    assert "readinessProbe" in c and "livenessProbe" in c and "startupProbe" in c


@needs_kubectl
def test_naive_variant_really_drops_the_protections():
    dep = of_kind(render("variants/naive"), "Deployment")[0]
    c = containers(dep)[0]
    assert "readinessProbe" not in c and "startupProbe" not in c and "lifecycle" not in c
    assert dep["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == "25%"


@needs_kubectl
def test_probes_variant_has_probes_but_no_prestop():
    c = containers(of_kind(render("variants/probes"), "Deployment")[0])[0]
    assert "readinessProbe" in c and "lifecycle" not in c


@needs_kubectl
def test_no_pdb_variant_has_no_pdb_and_dev_has_one():
    assert of_kind(render("variants/no-pdb"), "PodDisruptionBudget") == []
    assert len(of_kind(render("overlays/dev"), "PodDisruptionBudget")) == 1


@needs_kubectl
def test_prod_overlay_has_its_own_namespace_hpa_and_a_pdb_that_leaves_room():
    docs = render("overlays/prod")
    namespaces = {d["metadata"].get("namespace") for d in docs if d["kind"] != "Namespace"}
    assert namespaces == {"rollout-lab-prod"}
    assert of_kind(docs, "Namespace")[0]["metadata"]["name"] == "rollout-lab-prod"
    replicas = of_kind(docs, "Deployment")[0]["spec"]["replicas"]
    assert replicas >= 4
    assert of_kind(docs, "PodDisruptionBudget")[0]["spec"]["minAvailable"] < replicas
    assert len(of_kind(docs, "HorizontalPodAutoscaler")) == 1
