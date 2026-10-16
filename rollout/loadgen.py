"""Fixed-rate HTTP load generator. Standard library only.

Copied into the app image so it can run inside the cluster, where the NetworkPolicy
applies to it like any other client. Every request opens a new connection, so each one
is routed afresh by the Service. Prints one JSON summary on stdout and nothing else.

    python loadgen.py --url http://app/work --rate 40 --duration 45
"""
import argparse
import http.client
import json
import math
import socket
import sys
import threading
import time
from collections import Counter
from urllib.parse import urlsplit


def percentile(values, p):
    """Linear-interpolated percentile (p in 0..100) of a list of numbers."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def classify(exc):
    """Name the way a request failed, so refused and reset connections can be told apart."""
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                        http.client.RemoteDisconnected, http.client.IncompleteRead,
                        http.client.BadStatusLine)):
        return "reset"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    return "other"


def request_once(host, port, path, timeout):
    """One GET on a fresh connection. Returns (kind, milliseconds, version)."""
    start = time.monotonic()
    version = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", path, headers={"Connection": "close"})
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        try:
            version = json.loads(body).get("version")
        except (ValueError, AttributeError):
            pass
        kind = "ok" if resp.status == 200 else f"http_{resp.status}"
    except (OSError, http.client.HTTPException) as exc:
        kind = classify(exc)
    return kind, (time.monotonic() - start) * 1000.0, version


def run(url, rate, duration, workers, timeout):
    """Send `rate` requests per second for `duration` seconds. Returns a list of
    (seconds since start, kind, milliseconds, version) records."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 80
    path = parts.path or "/"
    records, lock = [], threading.Lock()
    interval = workers / float(rate)
    t0 = time.monotonic()

    def worker(i):
        nxt = t0 + i * interval / workers
        while True:
            now = time.monotonic()
            if nxt - t0 >= duration:
                return
            if now < nxt:
                time.sleep(nxt - now)
            kind, ms, version = request_once(host, port, path, timeout)
            with lock:
                records.append((nxt - t0, kind, ms, version))
            nxt += interval

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return records


def summarise(records, duration, bucket=1.0):
    """Reduce raw records to the numbers an experiment reports."""
    ok = [r for r in records if r[1] == "ok"]
    failed = [r for r in records if r[1] != "ok"]
    n_buckets = max(1, int(math.ceil(duration / bucket)))
    sent_per_s, failed_per_s = [0] * n_buckets, [0] * n_buckets
    for t, kind, _, _ in records:
        i = min(n_buckets - 1, int(t // bucket))
        sent_per_s[i] += 1
        if kind != "ok":
            failed_per_s[i] += 1
    lat = [r[2] for r in ok]
    fail_times = [r[0] for r in failed]
    return {
        "total": len(records),
        "ok": len(ok),
        "failed": len(failed),
        "failure_rate": (len(failed) / len(records)) if records else 0.0,
        "errors": dict(Counter(r[1] for r in failed)),
        "versions": dict(Counter(r[3] for r in records if r[3] is not None)),
        "latency_ms": {
            "p50": percentile(lat, 50), "p95": percentile(lat, 95),
            "p99": percentile(lat, 99), "max": max(lat) if lat else None,
        },
        "first_failure_s": min(fail_times) if fail_times else None,
        "last_failure_s": max(fail_times) if fail_times else None,
        "sent_per_s": sent_per_s,
        "failed_per_s": failed_per_s,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--rate", type=float, default=40.0, help="requests per second")
    ap.add_argument("--duration", type=float, default=45.0, help="seconds")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--timeout", type=float, default=2.0, help="per-request timeout, seconds")
    args = ap.parse_args(argv)
    records = run(args.url, args.rate, args.duration, args.workers, args.timeout)
    summary = summarise(records, args.duration)
    summary["config"] = {"url": args.url, "rate": args.rate, "duration": args.duration,
                         "workers": args.workers, "timeout": args.timeout}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
