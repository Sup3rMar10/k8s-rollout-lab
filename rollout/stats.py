"""Small statistics helpers for combining repeated runs. Standard library only."""
import math

from .loadgen import percentile  # noqa: F401  (re-exported)


def mean(values):
    return sum(values) / len(values) if values else None


def stdev(values):
    """Sample standard deviation; None with fewer than two values."""
    if len(values) < 2:
        return None
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def aggregate(summaries):
    """Combine the load-generator summaries of repeated runs of one configuration.

    The per-run failure counts are the primary result. Requests inside one failure burst
    are not independent, so no confidence interval is attached to the pooled rate."""
    per_run = [s["failed"] for s in summaries]
    total = sum(s["total"] for s in summaries)
    failed = sum(per_run)
    errors = {}
    for s in summaries:
        for kind, n in s["errors"].items():
            errors[kind] = errors.get(kind, 0) + n
    return {
        "runs": len(summaries),
        "requests": total,
        "failed": failed,
        "failure_rate": (failed / total) if total else 0.0,
        "failed_per_run": per_run,
        "mean_failed_per_run": mean(per_run),
        "min_failed_per_run": min(per_run) if per_run else None,
        "max_failed_per_run": max(per_run) if per_run else None,
        "runs_with_zero_failures": sum(1 for f in per_run if f == 0),
        "errors": errors,
    }


def summarise_numbers(values):
    """mean / min / max / sample stdev of a list, for durations and pod counts."""
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": mean(values), "min": min(values), "max": max(values),
            "stdev": stdev(values)}
