#!/usr/bin/env python3
"""nvlmapper_exporter.py — Prometheus exporter for switch-side intra-rack NVLink down-lanes.

Two jobs, both optional and env-gated:

  1. SWEEP   (only when NVLMAPPER_RUN_BINARY=true): on a slow cadence, invoke the
             vendor-supplied NVLMapper binary via run_nvlmapper.sh, which logs into
             each rack's NVSwitch tray BMCs (from config.yml) and writes fresh
             midfile__acp_down_connections.txt files under NVLMAPPER_OUTPUT_ROOT/nvlmapper/.
             If disabled, the midfiles are expected to already exist there (produced
             out-of-band by an operator / cron running the binary elsewhere).

  2. PARSE   (always): scan NVLMAPPER_OUTPUT_ROOT/nvlmapper/ on a fast cadence with
             parse_nvlmapper.collect() and serve the union of down-lane gauges plus
             exporter-level liveness on :NVLMAPPER_PORT (/metrics). Optionally mirror
             the same text to a node_exporter textfile collector dir.

parse_nvlmapper.collect() never raises; this harness additionally emits liveness so
"blank != healthy" (a rack with no down lanes still emits nvlmapper_source_mtime_seconds,
and the exporter emits nvlmapper_exporter_up / last-scan / sweep status).

Stdlib only for the exporter itself (runs in bare python:3.11-slim). Env:
  CLUSTER_NAME | NVLMAPPER_CLUSTER  Prometheus cluster= label        (default "cluster")
  NVLINK_FLAVOR                     NVLink flavor label               (default "" -> omitted)
  NVLMAPPER_OUTPUT_ROOT             root holding nvlmapper/ midfiles  (default "/data")
  NVLMAPPER_PORT                    HTTP listen port                  (default 9116)
  NVLMAPPER_SCRAPE_INTERVAL         seconds between parse passes      (default 60)
  NVLMAPPER_HEALTHZ_STALE           /healthz fails if no parse in Ns  (default 300)
  NVLMAPPER_TEXTFILE_DIR            if set, also write nvlmapper.prom here (default "")
  NVLMAPPER_RUN_BINARY              "true" -> run the vendor sweep     (default "false")
  NVLMAPPER_RUN_INTERVAL            seconds between vendor sweeps      (default 900)
  NVLMAPPER_RUNNER                  sweep script path (default /nvlmapper/run_nvlmapper.sh)
"""
import os
import sys
import time
import threading
import traceback
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_nvlmapper  # noqa: E402  (parser module lives next to this file)

CLUSTER = (os.environ.get("CLUSTER_NAME")
           or os.environ.get("NVLMAPPER_CLUSTER")
           or "cluster")
FLAVOR = os.environ.get("NVLINK_FLAVOR", "").strip()
OUTPUT_ROOT = os.environ.get("NVLMAPPER_OUTPUT_ROOT", "/data")
PORT = int(os.environ.get("NVLMAPPER_PORT", "9116"))
INTERVAL = float(os.environ.get("NVLMAPPER_SCRAPE_INTERVAL", "60"))
# /healthz fails once a parse pass hasn't COMPLETED within this many seconds — catches a scan
# thread stuck on a hung `hard`-mount NFS syscall (a Python-level timeout can't interrupt a blocked
# syscall, so a liveness probe that restarts the container is the only reliable recovery).
HEALTHZ_STALE = float(os.environ.get("NVLMAPPER_HEALTHZ_STALE", "300"))
TEXTFILE_DIR = os.environ.get("NVLMAPPER_TEXTFILE_DIR", "").strip()

RUN_BINARY = os.environ.get("NVLMAPPER_RUN_BINARY", "false").strip().lower() in ("1", "true", "yes")
RUN_INTERVAL = float(os.environ.get("NVLMAPPER_RUN_INTERVAL", "900"))
RUNNER = os.environ.get(
    "NVLMAPPER_RUNNER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_nvlmapper.sh"))

_lock = threading.Lock()
_cache = "# nvlmapper-exporter starting\n"
_collector_errors = 0
_last_scan_ts = 0.0
_last_scan_dur = 0.0
_scan_count = 0
_sweep_last_ts = 0.0
_sweep_last_rc = -1
_sweep_count = 0


def _base_labels():
    b = 'cluster="%s"' % parse_nvlmapper._esc(CLUSTER)
    if FLAVOR:
        b += ',flavor="%s"' % parse_nvlmapper._esc(FLAVOR)
    return b


def _harness_metrics(dur):
    bl = _base_labels()
    lines = [
        "# HELP nvlmapper_exporter_up 1 if the exporter's parse loop is running",
        "# TYPE nvlmapper_exporter_up gauge",
        "nvlmapper_exporter_up{%s} 1" % bl,
        "# HELP nvlmapper_scrape_duration_seconds wallclock of the last parse pass",
        "# TYPE nvlmapper_scrape_duration_seconds gauge",
        "nvlmapper_scrape_duration_seconds{%s} %.4f" % (bl, dur),
        "# HELP nvlmapper_last_scan_timestamp_seconds unix time of the last parse pass",
        "# TYPE nvlmapper_last_scan_timestamp_seconds gauge",
        "nvlmapper_last_scan_timestamp_seconds{%s} %d" % (bl, int(time.time())),
        "# HELP nvlmapper_collector_errors_total parse_nvlmapper.collect() failures",
        "# TYPE nvlmapper_collector_errors_total counter",
        "nvlmapper_collector_errors_total{%s} %d" % (bl, _collector_errors),
        "# HELP nvlmapper_sweep_enabled 1 if this exporter also runs the vendor NVLMapper binary",
        "# TYPE nvlmapper_sweep_enabled gauge",
        "nvlmapper_sweep_enabled{%s} %d" % (bl, 1 if RUN_BINARY else 0),
        "# HELP nvlmapper_sweep_last_timestamp_seconds unix time of the last vendor sweep (0 = never)",
        "# TYPE nvlmapper_sweep_last_timestamp_seconds gauge",
        "nvlmapper_sweep_last_timestamp_seconds{%s} %d" % (bl, int(_sweep_last_ts)),
        "# HELP nvlmapper_sweep_last_rc exit code of the last vendor sweep (-1 = never, 0 = ok)",
        "# TYPE nvlmapper_sweep_last_rc gauge",
        "nvlmapper_sweep_last_rc{%s} %d" % (bl, _sweep_last_rc),
    ]
    return "\n".join(lines)


def _write_textfile(text):
    """Atomically write the exposition text to NVLMAPPER_TEXTFILE_DIR/nvlmapper.prom."""
    if not TEXTFILE_DIR:
        return
    try:
        os.makedirs(TEXTFILE_DIR, exist_ok=True)
        final = os.path.join(TEXTFILE_DIR, "nvlmapper.prom")
        tmp = final + ".$%d" % os.getpid()
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, final)  # atomic; node_exporter never reads a half-written .prom
    except Exception:
        sys.stderr.write("textfile write failed:\n%s\n" % traceback.format_exc())


def parse_loop():
    global _cache, _collector_errors, _last_scan_ts, _last_scan_dur, _scan_count
    while True:
        t0 = time.time()
        body = ""
        try:
            body = parse_nvlmapper.collect(OUTPUT_ROOT)  # contract: never raises
        except Exception:
            _collector_errors += 1
            sys.stderr.write("collect() raised:\n%s\n" % traceback.format_exc())
        dur = time.time() - t0
        text = _harness_metrics(dur) + "\n" + (body or "") + "\n"
        with _lock:
            _cache = text
            _last_scan_ts = time.time()
            _last_scan_dur = dur
            _scan_count += 1
        _write_textfile(text)
        time.sleep(INTERVAL)


def sweep_once():
    """Invoke the vendor NVLMapper sweep wrapper; returns its exit code."""
    global _sweep_last_ts, _sweep_last_rc, _sweep_count
    try:
        rc = subprocess.call(["bash", RUNNER])
    except Exception:
        sys.stderr.write("sweep invocation failed:\n%s\n" % traceback.format_exc())
        rc = 1
    _sweep_last_ts = time.time()
    _sweep_last_rc = rc
    _sweep_count += 1
    return rc


def sweep_loop():
    while True:
        sweep_once()
        time.sleep(RUN_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            # unhealthy if the parse loop hasn't completed a pass recently (stuck on hung NFS) ->
            # liveness probe restarts the container. Uses last COMPLETED parse, not the served cache.
            age = time.time() - _last_scan_ts
            healthy = age < HEALTHZ_STALE
            body = ("ok scan_age=%ds" if healthy else "STALE scan_age=%ds") % int(age)
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())
            return
        if self.path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        with _lock:
            payload = _cache.encode("utf-8", "replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass  # quiet


def main():
    # Do NOT block startup on an initial scan — if NFS is slow at boot the container would never
    # serve and crashloop. Seed a grace timestamp so /healthz is healthy until the first real parse
    # completes, then let the background loops do all the work.
    global _last_scan_ts
    _last_scan_ts = time.time()
    threading.Thread(target=parse_loop, daemon=True).start()
    if RUN_BINARY:
        threading.Thread(target=sweep_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write(
        "nvlmapper-exporter serving :%d root=%s parse=%ss sweep=%s(%ss) textfile=%s\n"
        % (PORT, OUTPUT_ROOT, INTERVAL, RUN_BINARY, RUN_INTERVAL, TEXTFILE_DIR or "off"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
