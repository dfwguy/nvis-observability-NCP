#!/usr/bin/env python3
"""llmb-job-exporter — the simple per-Slurm-job scraper for validation/benchmark runs.

Every job the LLMB / dgxc-benchmarking harness runs writes ONE training stdout log:
    ${SLURM_JOB_LOG_DIR}/workloads/*/experiments/*/*/*/log-*_<JOBID>_0.out
This exporter globs those, tail-reads each (progress lines are at the END), and emits per-JOBID
Prometheus metrics. No NCCL plugin, no dump-interval env, no per-collective JSON — just the job's own
log, which always exists and always has the truth.

Two lines scraped per the spec:
  "Step Time : 15.29s GPU utilization: 588.4MODEL_TFLOP/s/GPU"   -> step_seconds, tflops_per_gpu
  "[ts] iteration  50/  50 | ... nan iterations: 0 |"            -> step, total_steps, nan_iterations
Hang detector: log-file mtime. RUNNING (per slurm) + log_age > threshold  => STALLED (dashboard gates
on llmb_job_log_age_seconds). Pass/fail authority stays with slurm (slurm_job_info / sacct).

Bounded + cached: only the last TAIL_BYTES of each log is read; a finished log (mtime unchanged) is
parsed once and cached, so scanning all historical jobs every 30s stays cheap. Stdlib only.

Config (all env-driven, NO site values hardcoded):
  SLURM_JOB_LOG_DIR    root under which validation-job logs + results land (was LLMB_ROOT).
  SLURM_LOG_GLOB       glob for the training stdout log; relative to the root unless absolute.
                       Default matches the LLMB harness tree; set to e.g. "**/log-*_*_0.out"
                       for a flatter layout.
  SLURM_REGISTRY_SQLITE  optional harness job registry (jobid -> workload/model/dtype/scale);
                       identity labels only, progress/perf always come from the log. Default:
                       <root>/.llmb/jobs.sqlite3 (skipped silently if absent).
  DGX_FLAVOR           cluster/platform label value (e.g. "dgx-superpod"); falls back to
                       CLUSTER_NAME, then "cluster". Never a hostname.
  LLMB_EXPORTER_PORT   listen port (9119). LLMB_EXPORTER_INTERVAL (30s).
  LLMB_EXPORTER_HEALTHZ_STALE (150s). LLMB_EXPORTER_TAIL_BYTES (24Ki).
"""
import glob
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Root under which the harness writes per-job logs. SLURM_JOB_LOG_DIR is the interview-driven
# name; LLMB_ROOT is accepted for backward compatibility.
ROOT = os.environ.get("SLURM_JOB_LOG_DIR") or os.environ.get("LLMB_ROOT", "/data/llmb")
PORT = int(os.environ.get("LLMB_EXPORTER_PORT", "9119"))
INTERVAL = float(os.environ.get("LLMB_EXPORTER_INTERVAL", "30"))
HEALTHZ_STALE = float(os.environ.get("LLMB_EXPORTER_HEALTHZ_STALE", "150"))
TAIL_BYTES = int(os.environ.get("LLMB_EXPORTER_TAIL_BYTES", str(24 * 1024)))
# Cluster/platform label — generalized from the old hardcoded value to an env var.
CLUSTER = os.environ.get("DGX_FLAVOR") or os.environ.get("CLUSTER_NAME") or "cluster"

# Default log layout is the LLMB / dgxc-benchmarking harness tree. Operators with a different
# layout override SLURM_LOG_GLOB (absolute glob, or one relative to the root).
_DEFAULT_GLOB = os.path.join("workloads", "*", "experiments", "*", "*", "*", "log-*_*_0.out")
_GLOB = os.environ.get("SLURM_LOG_GLOB", _DEFAULT_GLOB)
LOG_GLOB = _GLOB if os.path.isabs(_GLOB) else os.path.join(ROOT, _GLOB)
SQLITE = os.environ.get("SLURM_REGISTRY_SQLITE") or os.path.join(ROOT, ".llmb", "jobs.sqlite3")

# JOBID from the filename tail:  ..._<JOBID>_0.out
_JID_RE = re.compile(r"_(\d+)_0\.out$")
# the two scrape lines (spec)
_STEP_RE = re.compile(r"Step Time\s*:\s*([0-9.]+)s\s*GPU utilization:\s*([0-9.]+)MODEL_TFLOP/s/GPU")
_ITER_RE = re.compile(r"iteration\s+(\d+)/\s*(\d+)")
_NAN_RE = re.compile(r"nan iterations:\s*(\d+)")

_lock = threading.Lock()
_cache = "# llmb-job-exporter starting\n"
_last_scan_ts = 0.0
_last_scan_dur = 0.0
# parsed-log cache: path -> (mtime, size, parsed_dict). A finished log never changes -> parse once.
_LOGCACHE = {}


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _expname(path):
    """The experiment name dir encodes workload/dtype/scale, e.g.
    pretrain_deepseek_v3_fp8_mx_gpus512_tp1_pp2_...  (the dir two levels above the log file)."""
    return os.path.basename(os.path.dirname(path))


_SCALE_RE = re.compile(r"gpus(\d+)")
_DTYPE_RE = re.compile(r"_(nvfp4|bf16|fp16|fp32|fp8)_")


def _ident_from_path(path):
    """Fallback identity when the sqlite row is empty (harness hasn't populated it yet):
    workload from .../workloads/<workload>/..., model/dtype/scale from the experiment-name dir."""
    parts = path.split(os.sep)
    workload = ""
    if "workloads" in parts:
        i = parts.index("workloads")
        if i + 1 < len(parts):
            workload = parts[i + 1]
    exp = _expname(path)
    sc = _SCALE_RE.search(exp)
    dt = _DTYPE_RE.search(exp)
    model = workload[len("pretrain_"):] if workload.startswith("pretrain_") else workload
    return {"workload": workload, "model": model, "size": "",
            "dtype": (dt.group(1) if dt else ""), "scale": (sc.group(1) if sc else ""),
            "state": "", "exit_code": "", "node_list": ""}


def _parse_log(path):
    """Tail-read a training log and pull the LAST progress/perf lines. Never raises."""
    out = {"step": None, "total": None, "step_seconds": None, "tflops": None, "nan": None}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
            text = fh.read().decode("utf-8", "replace")
    except Exception:
        return None
    for m in _ITER_RE.finditer(text):
        out["step"], out["total"] = int(m.group(1)), int(m.group(2))   # last wins
    for m in _STEP_RE.finditer(text):
        out["step_seconds"], out["tflops"] = float(m.group(1)), float(m.group(2))
    nans = _NAN_RE.findall(text)
    if nans:
        out["nan"] = int(nans[-1])
    return out


def _sqlite_meta():
    """jobid -> {workload,model,size,dtype,scale,state,exit_code,node_list} from the harness registry.
    Status/perf columns are often NULL (harness parse step may not have run) — used only for identity
    labels; progress/perf come from the log."""
    meta = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % SQLITE, uri=True, timeout=3)
        for r in con.execute("select job_id,workload_key,model_name,model_size,dtype,scale,"
                             "slurm_state,exit_code,node_list from jobs"):
            meta[str(r[0])] = {"workload": r[1] or "", "model": r[2] or "", "size": r[3] or "",
                               "dtype": r[4] or "", "scale": str(r[5] or ""),
                               "state": r[6] or "", "exit_code": ("" if r[7] is None else str(r[7])),
                               "node_list": r[8] or ""}
        con.close()
    except Exception:
        sys.stderr.write("sqlite read failed:\n%s\n" % traceback.format_exc())
    return meta


def _labels(jid, m):
    return ('jobid="%s",cluster="%s",workload="%s",model="%s",size="%s",dtype="%s",scale="%s"'
            % (jid, CLUSTER, _esc(m.get("workload", "")), _esc(m.get("model", "")),
               _esc(m.get("size", "")), _esc(m.get("dtype", "")), _esc(m.get("scale", ""))))


def scan_once():
    global _LOGCACHE
    now = time.time()
    meta = _sqlite_meta()
    try:
        paths = glob.glob(LOG_GLOB, recursive=True)
    except Exception:
        paths = []
    # keep only the newest log per jobid (a job may have >1 het-group log; _0 is rank-0/main)
    by_jid = {}
    for p in paths:
        m = _JID_RE.search(os.path.basename(p))
        if not m:
            continue
        jid = m.group(1)
        try:
            mt = os.path.getmtime(p)
        except Exception:
            continue
        if jid not in by_jid or mt > by_jid[jid][0]:
            by_jid[jid] = (mt, p)

    new_logcache = {}
    rows = {"step": [], "total": [], "sec": [], "tflops": [], "nan": [], "age": [], "info": []}
    for jid, (mt, path) in by_jid.items():
        try:
            sz = os.path.getsize(path)
        except Exception:
            sz = 0
        cached = _LOGCACHE.get(path)
        if cached and cached[0] == mt and cached[1] == sz:
            parsed = cached[2]                       # unchanged log -> reuse
        else:
            parsed = _parse_log(path)
        if parsed is None:
            continue
        new_logcache[path] = (mt, sz, parsed)
        pm = _ident_from_path(path)
        sm = meta.get(jid, {})
        m = {k: (sm.get(k) or pm.get(k, "")) for k in
             ("workload", "model", "size", "dtype", "scale", "state", "exit_code", "node_list")}
        lbl = _labels(jid, m)
        age = int(now - mt)
        rows["age"].append('llmb_job_log_age_seconds{%s} %d' % (lbl, age))
        rows["info"].append('llmb_job_info{%s,state="%s",exit_code="%s"} 1'
                            % (lbl, _esc(m.get("state", "")), _esc(m.get("exit_code", ""))))
        if parsed["step"] is not None:
            rows["step"].append('llmb_job_step{%s} %d' % (lbl, parsed["step"]))
        if parsed["total"]:
            rows["total"].append('llmb_job_total_steps{%s} %d' % (lbl, parsed["total"]))
        if parsed["step_seconds"] is not None:
            rows["sec"].append('llmb_job_step_seconds{%s} %s' % (lbl, parsed["step_seconds"]))
        if parsed["tflops"] is not None:
            rows["tflops"].append('llmb_job_tflops_per_gpu{%s} %s' % (lbl, parsed["tflops"]))
        if parsed["nan"] is not None:
            rows["nan"].append('llmb_job_nan_iterations{%s} %d' % (lbl, parsed["nan"]))
    _LOGCACHE = new_logcache

    blocks = []
    fams = [
        ("llmb_job_step", "current training iteration", rows["step"]),
        ("llmb_job_total_steps", "total training iterations", rows["total"]),
        ("llmb_job_step_seconds", "seconds per training step (last)", rows["sec"]),
        ("llmb_job_tflops_per_gpu", "MODEL_TFLOP/s per GPU (last)", rows["tflops"]),
        ("llmb_job_nan_iterations", "nan iterations so far (>0 = numerical trouble)", rows["nan"]),
        ("llmb_job_log_age_seconds", "age of the training log; large while RUNNING = STALLED", rows["age"]),
        ("llmb_job_info", "per-job identity + last-known slurm state/exit (value 1)", rows["info"]),
    ]
    for name, help_, lines in fams:
        if lines:
            blocks.append("# HELP %s %s" % (name, help_))
            blocks.append("# TYPE %s gauge" % name)
            blocks.extend(sorted(lines))
    blocks.append("# HELP llmb_job_count number of jobs with a training log")
    blocks.append("# TYPE llmb_job_count gauge")
    blocks.append('llmb_job_count{cluster="%s"} %d' % (CLUSTER, len(by_jid)))
    return "\n".join(blocks) + "\n"


def _harness(dur):
    return ("# HELP llmb_exporter_up 1 if the scrape loop is running\n"
            "# TYPE llmb_exporter_up gauge\n"
            'llmb_exporter_up{cluster="%s"} 1\n'
            "# HELP llmb_last_scan_timestamp_seconds unix time of last completed scan\n"
            "# TYPE llmb_last_scan_timestamp_seconds gauge\n"
            'llmb_last_scan_timestamp_seconds{cluster="%s"} %d\n'
            "# HELP llmb_scan_duration_seconds wallclock of the last scan\n"
            "# TYPE llmb_scan_duration_seconds gauge\n"
            'llmb_scan_duration_seconds{cluster="%s"} %.3f\n'
            % (CLUSTER, CLUSTER, int(time.time()), CLUSTER, dur))


def loop():
    global _cache, _last_scan_ts, _last_scan_dur
    while True:
        t0 = time.time()
        try:
            body = scan_once()
        except Exception:
            body = ""
            sys.stderr.write("scan_once failed:\n%s\n" % traceback.format_exc())
        dur = time.time() - t0
        with _lock:
            _cache = _harness(dur) + body
            _last_scan_ts = time.time()
            _last_scan_dur = dur
        time.sleep(INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            age = time.time() - _last_scan_ts
            ok = age < HEALTHZ_STALE
            b = (("ok" if ok else "STALE") + " scan_age=%ds" % int(age)).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
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
        pass


def main():
    global _last_scan_ts
    _last_scan_ts = time.time()
    threading.Thread(target=loop, daemon=True).start()
    sys.stderr.write("llmb-job-exporter serving :%d root=%s interval=%ss\n" % (PORT, ROOT, INTERVAL))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        ROOT = sys.argv[1]
        LOG_GLOB = os.path.join(ROOT, _DEFAULT_GLOB)
        SQLITE = os.path.join(ROOT, ".llmb", "jobs.sqlite3")
        print(scan_once())
    else:
        main()
