"""Command line entry point: python -m rollout <command>."""
import argparse
import json
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m rollout", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("cluster-up", help="create the kind cluster, build and load the image")
    up.add_argument("--small", action="store_true",
                    help="1 control-plane + 1 worker (kind-config-small.yaml) instead of 1 + 2")
    sub.add_parser("cluster-down", help="delete the kind cluster")

    ex = sub.add_parser("experiment", help="run one experiment against the cluster")
    ex.add_argument("name", choices=["rollout", "badrelease", "drain", "netpol", "validate", "all"],
                    help="all = validate, netpol, rollout, badrelease (drain is opt-in and was not run for the results)")
    ex.add_argument("--reps", type=int, default=None, help="repetitions per configuration")

    sub.add_parser("report", help="rebuild the chart and the README results block from results/summary.json")
    sub.add_parser("lint", help="check the manifests against the rules in tests/test_manifests.py")

    args = ap.parse_args(argv)

    if args.cmd == "cluster-up":
        from . import cluster
        cluster.up(small=args.small)
    elif args.cmd == "cluster-down":
        from . import cluster
        cluster.down()
    elif args.cmd == "experiment":
        from . import experiments
        result = experiments.run(args.name, args.reps)
        print(json.dumps(result, indent=2, default=str)[:4000])
    elif args.cmd == "report":
        from . import report
        report.build()
    elif args.cmd == "lint":
        import subprocess
        return subprocess.call([sys.executable, "-m", "pytest", "-q", "tests/test_manifests.py"])
    return 0
