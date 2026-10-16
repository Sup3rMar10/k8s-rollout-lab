"""Tiny HTTP service for the rollout experiments. Standard library only.

Endpoints
  /healthz  liveness: 200 as long as the process is up
  /readyz   readiness: 200 once started and not draining; 503 while draining or when BAD_RELEASE=1
  /work     does WORK_MS of pretend work and reports which pod and version answered

Environment
  PORT           listen port (default 8080)
  STARTUP_DELAY  seconds of "initialisation" before the port is opened (default 0)
  WORK_MS        milliseconds each /work request takes (default 20)
  VERSION        string reported by /work (default 1.0.0)
  BAD_RELEASE    "1" simulates a broken release: /work answers 500 and /readyz answers 503

SIGTERM behaviour: stop accepting new connections, finish the requests already in
flight, exit 0. Both configurations in the experiments run this same code, so any
failed request during a rollout comes from the manifest, not from the app.
"""
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


class State:
    def __init__(self):
        self.draining = False
        self.bad_release = os.environ.get("BAD_RELEASE", "0") == "1"
        self.version = os.environ.get("VERSION", "1.0.0")
        self.work_s = env_float("WORK_MS", 20) / 1000.0
        self.pod = os.environ.get("HOSTNAME", socket.gethostname())


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # one request per connection, like independent clients

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, {"status": "alive"})
        elif path == "/readyz":
            if STATE.draining:
                self._send(503, {"status": "draining"})
            elif STATE.bad_release:
                self._send(503, {"status": "bad release"})
            else:
                self._send(200, {"status": "ready"})
        elif path == "/work":
            time.sleep(STATE.work_s)
            if STATE.bad_release:
                self._send(500, {"error": "bad release", "version": STATE.version, "pod": STATE.pod})
            else:
                self._send(200, {"version": STATE.version, "pod": STATE.pod})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # keep the logs for lifecycle events only
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = False  # server_close() waits for in-flight requests
    request_queue_size = 128


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {STATE.pod} {msg}", flush=True)


def main():
    port = int(os.environ.get("PORT", "8080"))
    delay = env_float("STARTUP_DELAY", 0)
    server_box = {}

    def on_term(signum, frame):
        STATE.draining = True
        log("SIGTERM received, draining")
        server = server_box.get("server")
        if server is None:  # still initialising: nothing to drain
            sys.exit(0)
        threading.Thread(target=server.shutdown, daemon=True).start()

    # Installed before the startup delay: PID 1 ignores SIGTERM without a handler.
    signal.signal(signal.SIGTERM, on_term)

    log(f"initialising for {delay:g}s (version {STATE.version}, bad_release={STATE.bad_release})")
    time.sleep(delay)
    server = Server(("0.0.0.0", port), Handler)
    server_box["server"] = server
    log(f"listening on :{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log("stopped")


if __name__ == "__main__":
    main()
