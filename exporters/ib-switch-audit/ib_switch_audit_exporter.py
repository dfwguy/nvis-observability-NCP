#!/usr/bin/env python3
"""NVIS Platform -- IB switch audit / firmware exporter (:9117).

Reads two ground-truth CSVs (produced on the observability host) and exposes an
InfiniBand switch *identity + audit* view for Prometheus / VictoriaMetrics:

  * ``ufm_switches_new.csv``            -- UFM REST inventory (authoritative for
    role/model/fw/sw/state; schema: ip,name(=guid),system_name,role,state,model,
    fw_version,sw_version,guid).
  * ``nvos_all_switches_audit_*.csv``   -- the SSH/DNS/UFM cross-audit. The
    exporter globs these and picks the newest by ``audit_timestamp_utc``. It
    supplies hostname/ssh/resolution status and the reported-vs-expected
    hostname mismatch signature.

Join key: IP (present in both), with GUID carried as the stable cross-source
identity. The UFM inventory is treated as the switch set of record; audit rows
are merged in by IP.

Namespace coordination: the topology "info-metrics" exporter (ib-identity) also
publishes a switch identity bridge named ``ib_switch_info`` carrying topology
fields (su, rack, link maps, ...). To avoid a metric-name collision this exporter
owns the *audit / firmware* fields and publishes them under
``ib_switch_audit_info`` (NOT ``ib_switch_info``). All derived audit gauges below
have unique names and do not collide.

Metrics emitted (all carry a ``cluster`` label):
  * ib_switch_audit_info{ip,system_name,expected_hostname,role,state,model,
        fw_version,sw_version,guid} = 1
        -- one series per switch; the identity/firmware table + join bridge.
  * ib_switch_hostname_mismatch_total = <count of hostname_status=MISMATCH>
  * ib_switch_hostname_mismatch{ip,system_name,reported,expected,role} = 1
        -- one per mismatching device (drilldown table).
  * ib_switch_by_role{role} = <count>            (e.g. core=spine / tor=leaf)
  * ib_switch_fw_drift = <distinct fw_version count>   (alert on >1)
  * ib_switch_sw_drift = <distinct populated sw_version count>
  * ib_switch_swversion_coverage = <switches with a real sw_version>
  * ib_switch_ssh_reachable_total = <ssh_status=CONNECTED count>
  * ib_switch_total = <switch count>
  * ib_switch_audit_age_seconds = now - newest audit_timestamp_utc
  * ib_switch_audit_exporter_up = 1 while serving
  * ib_switch_audit_reload_total{result="ok|error"} = data reload counter
  * ib_switch_audit_last_reload_timestamp = unix epoch of last successful reload

Environment variables (all site values come from the .env the installer writes)
---------------------
LISTEN_PORT / PORT   HTTP listen port (default 9117).
LISTEN_ADDR          Bind address (default 0.0.0.0).
CLUSTER_NAME         Value of the ``cluster`` label (default "cluster").
UFM_CSV              Path to the UFM inventory CSV
                     (default /data/ufm_switches_new.csv).
AUDIT_GLOB           Glob for the NVOS audit CSVs; newest by audit_timestamp_utc
                     is used (default /data/nvos_all_switches_audit_*.csv).
REFRESH_INTERVAL     Seconds between CSV re-reads (default 60).
"""
import csv
import glob
import os
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
LISTEN_PORT = int(os.environ.get("LISTEN_PORT") or os.environ.get("PORT") or 9117)
LISTEN_ADDR = os.environ.get("LISTEN_ADDR", "0.0.0.0")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "cluster")
UFM_CSV = os.environ.get("UFM_CSV", "/data/ufm_switches_new.csv")
AUDIT_GLOB = os.environ.get("AUDIT_GLOB", "/data/nvos_all_switches_audit_*.csv")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "60"))


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Prometheus text helpers
# --------------------------------------------------------------------------- #
def _esc(v) -> str:
    """Escape a Prometheus label value."""
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(pairs) -> str:
    return ",".join(f'{k}="{_esc(v)}"' for k, v in pairs)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
class Snapshot:
    """Immutable-ish view built from one read of both CSVs."""

    def __init__(self):
        self.switches = []          # list of merged dicts
        self.audit_epoch = None     # newest audit_timestamp_utc as epoch secs
        self.audit_file = None
        self.ok = False


def _parse_ts(s: str):
    """Parse an ISO-8601 UTC timestamp like 2026-01-01T13:41:56Z -> epoch."""
    if not s:
        return None
    try:
        s2 = s.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except ValueError:
        return None


def _read_ufm(path: str):
    """Return {ip: row} from the UFM inventory CSV."""
    out = {}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            ip = (row.get("ip") or "").strip()
            if ip:
                out[ip] = {k: (v.strip() if isinstance(v, str) else v)
                           for k, v in row.items()}
    return out


def _newest_audit(glob_pat: str):
    """Pick the newest audit CSV by max audit_timestamp_utc; fall back to the
    lexically-greatest filename (the timestamps are embedded in the names).
    Returns (path, {ip: row}, newest_epoch)."""
    files = glob.glob(glob_pat)
    if not files:
        return None, {}, None
    # Filenames embed a sortable UTC stamp; use that to shortlist, then confirm
    # by the in-file audit_timestamp_utc column.
    files.sort()
    best_path, best_rows, best_epoch = None, {}, None
    for path in files:
        try:
            with open(path, newline="") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            continue
        if not rows:
            continue
        epochs = [e for e in (_parse_ts(r.get("audit_timestamp_utc", "")) for r in rows)
                  if e is not None]
        file_epoch = max(epochs) if epochs else 0.0
        if best_epoch is None or file_epoch >= best_epoch:
            by_ip = {}
            for r in rows:
                ip = (r.get("ip") or "").strip()
                if ip:
                    by_ip[ip] = {k: (v.strip() if isinstance(v, str) else v)
                                 for k, v in r.items()}
            best_path, best_rows, best_epoch = path, by_ip, file_epoch
    return best_path, best_rows, (best_epoch if best_epoch else None)


def build_snapshot() -> Snapshot:
    snap = Snapshot()
    ufm = _read_ufm(UFM_CSV)
    audit_path, audit, audit_epoch = _newest_audit(AUDIT_GLOB)
    snap.audit_file = audit_path
    snap.audit_epoch = audit_epoch

    # The UFM inventory is the switch set of record. Merge audit fields by IP.
    ips = list(ufm.keys())
    # Include any audit-only IPs (defensive; normally identical sets).
    for ip in audit:
        if ip not in ufm:
            ips.append(ip)

    for ip in ips:
        u = ufm.get(ip, {})
        a = audit.get(ip, {})
        guid = (u.get("guid") or u.get("name") or a.get("ufm_guid") or "").strip()
        # role: UFM inventory is authoritative (the newest audit CSV can carry
        # an empty ufm_role column); fall back to audit's ufm_role.
        role = (u.get("role") or a.get("ufm_role") or "").strip()
        state = (u.get("state") or a.get("ufm_state") or "").strip()
        hostname = (u.get("system_name") or a.get("ufm_system_name") or "").strip()
        expected = (a.get("expected_hostname") or "").strip()
        reported = (a.get("reported_hostname") or "").strip()
        snap.switches.append({
            "ip": ip,
            "guid": guid,
            "system_name": hostname,
            "expected_hostname": expected,
            "reported_hostname": reported,
            "role": role,
            "state": state,
            "model": (u.get("model") or a.get("ufm_description") or "").strip(),
            "fw_version": (u.get("fw_version") or "").strip(),
            "sw_version": (u.get("sw_version") or "").strip(),
            "hostname_status": (a.get("hostname_status") or "").strip(),
            "ssh_status": (a.get("ssh_status") or "").strip(),
            "resolution_status": (a.get("resolution_status") or "").strip(),
            "ufm_vs_switch": (a.get("ufm_vs_switch") or "").strip(),
        })
    snap.ok = len(snap.switches) > 0
    return snap


# --------------------------------------------------------------------------- #
# Shared state (updated by the refresh thread, read by HTTP handler)
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_snap = Snapshot()
_reload_ok = 0
_reload_err = 0
_last_reload_epoch = 0.0


def refresh_loop():
    global _snap, _reload_ok, _reload_err, _last_reload_epoch
    while True:
        try:
            new = build_snapshot()
            with _lock:
                _snap = new
                _reload_ok += 1
                _last_reload_epoch = time.time()
            log(f"reload ok: {len(new.switches)} switches from ufm={UFM_CSV} "
                f"audit={new.audit_file}")
        except Exception as exc:  # noqa: BLE001 -- keep serving stale on error
            with _lock:
                _reload_err += 1
            log(f"reload ERROR: {exc!r}")
        time.sleep(REFRESH_INTERVAL)


# --------------------------------------------------------------------------- #
# Metric rendering
# --------------------------------------------------------------------------- #
def render_metrics() -> str:
    with _lock:
        snap = _snap
        reload_ok = _reload_ok
        reload_err = _reload_err
        last_reload = _last_reload_epoch

    C = CLUSTER_NAME
    lines = []

    def emit(name, labels_pairs, value):
        base = [("cluster", C)] + list(labels_pairs)
        lines.append(f"{name}{{{_labels(base)}}} {value}")

    # ---- exporter self / freshness ----
    lines.append("# HELP ib_switch_audit_exporter_up 1 while the exporter is serving.")
    lines.append("# TYPE ib_switch_audit_exporter_up gauge")
    emit("ib_switch_audit_exporter_up", [], 1)

    lines.append("# HELP ib_switch_audit_reload_total CSV reload attempts by result.")
    lines.append("# TYPE ib_switch_audit_reload_total counter")
    emit("ib_switch_audit_reload_total", [("result", "ok")], reload_ok)
    emit("ib_switch_audit_reload_total", [("result", "error")], reload_err)

    lines.append("# HELP ib_switch_audit_last_reload_timestamp Unix epoch of last successful CSV reload.")
    lines.append("# TYPE ib_switch_audit_last_reload_timestamp gauge")
    emit("ib_switch_audit_last_reload_timestamp", [], f"{last_reload:.0f}")

    # audit freshness: now - newest audit_timestamp_utc
    lines.append("# HELP ib_switch_audit_age_seconds Age of the newest NVOS audit snapshot in seconds.")
    lines.append("# TYPE ib_switch_audit_age_seconds gauge")
    if snap.audit_epoch:
        age = max(0.0, time.time() - snap.audit_epoch)
        emit("ib_switch_audit_age_seconds", [], f"{age:.0f}")

    switches = snap.switches

    # ---- identity / firmware table + join bridge ----
    lines.append("# HELP ib_switch_audit_info Switch identity + firmware/software audit fields (value always 1). Owns audit/fw fields; topology info-metrics owns ib_switch_info.")
    lines.append("# TYPE ib_switch_audit_info gauge")
    for s in switches:
        emit("ib_switch_audit_info", [
            ("ip", s["ip"]),
            ("system_name", s["system_name"]),
            ("expected_hostname", s["expected_hostname"]),
            ("role", s["role"]),
            ("state", s["state"]),
            ("model", s["model"]),
            ("fw_version", s["fw_version"]),
            ("sw_version", s["sw_version"]),
            ("guid", s["guid"]),
        ], 1)

    # ---- hostname mismatch ----
    mism = [s for s in switches if s["hostname_status"].upper() == "MISMATCH"]
    lines.append("# HELP ib_switch_hostname_mismatch_total Count of switches whose reported hostname disagrees with the expected/inventory hostname.")
    lines.append("# TYPE ib_switch_hostname_mismatch_total gauge")
    emit("ib_switch_hostname_mismatch_total", [], len(mism))

    lines.append("# HELP ib_switch_hostname_mismatch Per-device hostname mismatch (value 1); reported vs expected.")
    lines.append("# TYPE ib_switch_hostname_mismatch gauge")
    for s in mism:
        emit("ib_switch_hostname_mismatch", [
            ("ip", s["ip"]),
            ("system_name", s["system_name"]),
            ("reported", s["reported_hostname"]),
            ("expected", s["expected_hostname"]),
            ("role", s["role"]),
        ], 1)

    # ---- role counts ----
    role_counts = {}
    for s in switches:
        r = s["role"] or "unknown"
        role_counts[r] = role_counts.get(r, 0) + 1
    lines.append("# HELP ib_switch_by_role Switch count by UFM role (core=spine / tor=leaf).")
    lines.append("# TYPE ib_switch_by_role gauge")
    for r, n in sorted(role_counts.items()):
        emit("ib_switch_by_role", [("role", r)], n)

    # ---- firmware / software drift ----
    fw_set = {s["fw_version"] for s in switches if s["fw_version"]}
    lines.append("# HELP ib_switch_fw_drift Distinct firmware versions across the fleet (alert on >1).")
    lines.append("# TYPE ib_switch_fw_drift gauge")
    emit("ib_switch_fw_drift", [], len(fw_set))

    # populated sw versions = anything that is not empty / N/A
    def _sw_real(v):
        return bool(v) and v.upper() not in ("N/A", "NA", "NONE", "UNKNOWN")

    sw_set = {s["sw_version"] for s in switches if _sw_real(s["sw_version"])}
    sw_cov = sum(1 for s in switches if _sw_real(s["sw_version"]))
    lines.append("# HELP ib_switch_sw_drift Distinct populated NVOS software versions across the fleet.")
    lines.append("# TYPE ib_switch_sw_drift gauge")
    emit("ib_switch_sw_drift", [], len(sw_set))
    lines.append("# HELP ib_switch_swversion_coverage Switches reporting a real NVOS sw_version (UFM REST returns N/A for most).")
    lines.append("# TYPE ib_switch_swversion_coverage gauge")
    emit("ib_switch_swversion_coverage", [], sw_cov)

    # ---- ssh reachability / totals ----
    ssh_ok = sum(1 for s in switches if s["ssh_status"].upper() == "CONNECTED")
    lines.append("# HELP ib_switch_ssh_reachable_total Switches with ssh_status=CONNECTED in the newest audit.")
    lines.append("# TYPE ib_switch_ssh_reachable_total gauge")
    emit("ib_switch_ssh_reachable_total", [], ssh_ok)

    lines.append("# HELP ib_switch_total Total switches in the UFM inventory.")
    lines.append("# TYPE ib_switch_total gauge")
    emit("ib_switch_total", [], len(switches))

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        return

    def do_GET(self):
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        body = render_metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    log(f"NVIS IB switch-audit exporter starting (addr={LISTEN_ADDR}, "
        f"port={LISTEN_PORT}, ufm={UFM_CSV}, audit_glob={AUDIT_GLOB}, "
        f"refresh={REFRESH_INTERVAL}s, cluster={CLUSTER_NAME})")
    # Prime once synchronously so the first scrape is populated.
    global _snap, _reload_ok, _reload_err, _last_reload_epoch
    try:
        _snap = build_snapshot()
        _reload_ok += 1
        _last_reload_epoch = time.time()
        log(f"initial load: {len(_snap.switches)} switches, audit={_snap.audit_file}")
    except Exception as exc:  # noqa: BLE001
        _reload_err += 1
        log(f"initial load ERROR (will retry in loop): {exc!r}")
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = ThreadingServer((LISTEN_ADDR, LISTEN_PORT), Handler)
    log(f"Metrics available at http://{LISTEN_ADDR}:{LISTEN_PORT}/metrics")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
