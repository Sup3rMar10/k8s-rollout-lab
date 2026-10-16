import http.client
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app" / "server.py"
spec = importlib.util.spec_from_file_location("lab_server", APP)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    c.request("GET", path)
    r = c.getresponse()
    return r.status, json.loads(r.read())


@pytest.fixture()
def live():
    server.STATE.draining = False
    server.STATE.bad_release = False
    server.STATE.work_s = 0.0
    srv = server.Server(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()
    srv.server_close()
    server.STATE.draining = False
    server.STATE.bad_release = False


def test_healthy_service_answers_everywhere(live):
    assert get(live, "/healthz") == (200, {"status": "alive"})
    assert get(live, "/readyz")[0] == 200
    status, body = get(live, "/work")
    assert status == 200 and body["version"] and body["pod"]


def test_unknown_path_is_404(live):
    assert get(live, "/nope")[0] == 404


def test_bad_release_fails_readiness_and_work_but_stays_alive(live):
    server.STATE.bad_release = True
    assert get(live, "/healthz")[0] == 200
    assert get(live, "/readyz")[0] == 503
    status, body = get(live, "/work")
    assert status == 500 and body["error"] == "bad release"


def test_draining_fails_readiness_but_not_liveness(live):
    server.STATE.draining = True
    assert get(live, "/readyz") == (503, {"status": "draining"})
    assert get(live, "/healthz")[0] == 200


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(port, **env):
    full = dict(os.environ, PORT=str(port), WORK_MS="0", **{k: str(v) for k, v in env.items()})
    return subprocess.Popen([sys.executable, str(APP)], env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def _wait_listening(port, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return time.monotonic()
        except OSError:
            time.sleep(0.05)
    raise AssertionError("server never started listening")


def test_startup_delay_keeps_the_port_closed_until_it_elapses():
    port = _free_port()
    proc = _spawn(port, STARTUP_DELAY=1.0)
    try:
        t0 = time.monotonic()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        up = _wait_listening(port)
        assert up - t0 >= 0.8
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_sigterm_finishes_in_flight_request_then_exits_zero():
    port = _free_port()
    proc = _spawn(port, WORK_MS=800)
    try:
        _wait_listening(port)
        result = {}

        def slow():
            result["r"] = get(port, "/work")

        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        t.join(5)
        assert result["r"][0] == 200  # the in-flight request was not cut off
        assert proc.wait(5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_sigterm_during_startup_exits_instead_of_hanging():
    port = _free_port()
    proc = _spawn(port, STARTUP_DELAY=30)
    try:
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
