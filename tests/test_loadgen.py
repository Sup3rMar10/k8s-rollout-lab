import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rollout import loadgen, stats


def test_percentile_interpolates_and_handles_edges():
    assert loadgen.percentile([], 50) is None
    assert loadgen.percentile([7], 99) == 7.0
    assert loadgen.percentile([1, 2, 3, 4], 50) == 2.5
    assert loadgen.percentile([4, 1, 3, 2], 100) == 4.0
    assert loadgen.percentile(list(range(101)), 95) == pytest.approx(95.0)


@pytest.mark.parametrize("exc, kind", [
    (ConnectionRefusedError(), "refused"),
    (ConnectionResetError(), "reset"),
    (BrokenPipeError(), "reset"),
    (http.client.RemoteDisconnected("x"), "reset"),
    (TimeoutError(), "timeout"),
    (socket.timeout(), "timeout"),
    (OSError("boom"), "other"),
])
def test_classify_names_the_failure(exc, kind):
    assert loadgen.classify(exc) == kind


def test_summarise_counts_buckets_and_latency():
    records = [
        (0.1, "ok", 10.0, "1"), (0.6, "ok", 30.0, "1"),
        (1.2, "refused", 1.0, None), (1.9, "http_500", 20.0, "1"),
        (2.5, "ok", 20.0, "1"),
    ]
    s = loadgen.summarise(records, duration=3.0)
    assert (s["total"], s["ok"], s["failed"]) == (5, 3, 2)
    assert s["failure_rate"] == pytest.approx(0.4)
    assert s["errors"] == {"refused": 1, "http_500": 1}
    assert s["sent_per_s"] == [2, 2, 1] and s["failed_per_s"] == [0, 2, 0]
    assert s["first_failure_s"] == 1.2 and s["last_failure_s"] == 1.9
    assert s["latency_ms"]["p50"] == 20.0 and s["latency_ms"]["max"] == 30.0
    assert s["versions"] == {"1": 4}


def test_summarise_empty_run_does_not_divide_by_zero():
    s = loadgen.summarise([], duration=2.0)
    assert s["total"] == 0 and s["failure_rate"] == 0.0 and s["latency_ms"]["p50"] is None


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"version": "t"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_run_against_a_live_server_has_no_failures_and_the_right_volume():
    srv = HTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        records = loadgen.run(f"http://127.0.0.1:{srv.server_port}/work", rate=40, duration=1.0,
                              workers=8, timeout=2)
    finally:
        srv.shutdown()
    s = loadgen.summarise(records, 1.0)
    assert s["failed"] == 0
    assert 30 <= s["total"] <= 44
    assert s["versions"] == {"t": s["total"]}


def test_run_against_a_closed_port_counts_every_request_as_failed():
    records = loadgen.run(f"http://127.0.0.1:{_free_port()}/work", rate=20, duration=0.5, workers=4, timeout=1)
    s = loadgen.summarise(records, 0.5)
    assert s["total"] > 0 and s["ok"] == 0
    # Linux refuses a closed localhost port at once; Windows retries the SYN, so the 1 s timeout
    # fires first. Either way every request must be counted as a failure of one of these kinds.
    assert set(s["errors"]) <= {"refused", "timeout"}
    assert s["failed"] == s["total"]


def test_aggregate_combines_runs_and_keeps_per_run_counts():
    runs = [
        {"total": 100, "failed": 10, "errors": {"refused": 8, "timeout": 2}},
        {"total": 100, "failed": 0, "errors": {}},
        {"total": 100, "failed": 5, "errors": {"refused": 5}},
    ]
    a = stats.aggregate(runs)
    assert a["failed_per_run"] == [10, 0, 5]
    assert a["failed"] == 15 and a["requests"] == 300
    assert a["failure_rate"] == pytest.approx(0.05)
    assert a["runs_with_zero_failures"] == 1
    assert a["errors"] == {"refused": 13, "timeout": 2}
    assert (a["min_failed_per_run"], a["max_failed_per_run"]) == (0, 10)


def test_summarise_numbers():
    s = stats.summarise_numbers([2.0, 4.0, 6.0])
    assert s["mean"] == 4.0 and s["min"] == 2.0 and s["max"] == 6.0 and s["stdev"] == pytest.approx(2.0)
    assert stats.summarise_numbers([]) == {"n": 0}
    assert stats.stdev([5.0]) is None
