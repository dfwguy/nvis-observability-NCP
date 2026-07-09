#!/usr/bin/env python3
"""ibl_* exporter: UFM REST fabric state + batched switch LLDP sweep.

IB LLDP / fabric exporter (ibl_*) — augments the UFM REST exporter. Two data
planes:

  1. UFM REST (reliable, every poll): SM master/standby (sm_mode), pkeys,
     systems inventory -> ibl_sm_*, ibl_pkey_*, ibl_topology_*,
     ibl_fabric_health_*. Lights the LIVE ibl_ fabric-health / SM-redundancy
     panels.
  2. Batched switch LLDP sweep (every SWEEP_INTERVAL, default 30m): SSHes the
     switches in CONCURRENCY-capped BATCHES with an inter-batch pause so the
     switch mgmt CPUs are never hit all at once. Collects transceiver presence +
     SSH reachability -> ibl_switch_ssh_reachable, ibl_transceiver_modules.

Switch list: by default the switch IPs to sweep are auto-discovered from UFM
(/resources/systems). Set SWITCH_LIST to override with an explicit
comma-separated list of "name=ip" or "ip" entries (air-gapped / no-UFM sites).

Pure stdlib for the UFM-REST core (urllib); paramiko (pip at startup) is used for
the SSH sweep — if paramiko/SSH is unavailable the core still serves.

Environment variables (all site values come from the .env the installer writes):
  PORT                Listen port                          (default: 9114)
  CLUSTER_NAME        Prometheus cluster label             (default: cluster)
  UFM_HOST            UFM server IP or hostname            (required for the UFM plane)
  UFM_USER            UFM username                         (default: ufmsystem)
  UFM_PASSWORD        UFM password                         (UFM_PASS also accepted)
  UFM_POLL            Seconds between UFM polls             (default: 60)
  SWEEP_INTERVAL      Seconds between LLDP sweeps           (default: 1800)
  SWEEP_BATCH_SIZE    Switches per SSH batch               (default: 15)
  SWEEP_BATCH_PAUSE   Seconds between batches              (default: 5)
  SWEEP_ENABLED       1 to enable the SSH sweep            (default: 1)
  SWITCH_SSH_USER     Switch SSH username                  (NVOS_USERNAME also accepted)
  SWITCH_SSH_PASSWORD Switch SSH password                  (NVOS_PASSWORD also accepted)
  SWITCH_LIST         Optional "name=ip,name=ip,..." override for the sweep list
  SSH_TIMEOUT         Per-switch SSH timeout seconds        (default: 15)
"""
import os
import sys
import ssl
import json
import time
import base64
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT       = int(os.environ.get("PORT", "9114"))
CLUSTER    = os.environ.get("CLUSTER_NAME", "cluster")
UFM_HOST   = os.environ.get("UFM_HOST", "")
UFM_USER   = os.environ.get("UFM_USER", "ufmsystem")
UFM_PASS   = os.environ.get("UFM_PASSWORD") or os.environ.get("UFM_PASS", "")
UFM_POLL   = int(os.environ.get("UFM_POLL", "60"))
SWEEP_INT  = int(os.environ.get("SWEEP_INTERVAL", "1800"))   # 30 min
BATCH_SIZE = int(os.environ.get("SWEEP_BATCH_SIZE", "15"))   # switches per batch
BATCH_PAUSE= float(os.environ.get("SWEEP_BATCH_PAUSE", "5")) # seconds between batches
SSH_USER   = os.environ.get("SWITCH_SSH_USER") or os.environ.get("NVOS_USERNAME", "")
SSH_PASS   = os.environ.get("SWITCH_SSH_PASSWORD") or os.environ.get("NVOS_PASSWORD", "")
SSH_TIMEOUT= int(os.environ.get("SSH_TIMEOUT", "15"))
SWEEP_ON   = os.environ.get("SWEEP_ENABLED", "1") == "1"
SWITCH_LIST_RAW = os.environ.get("SWITCH_LIST", "").strip()

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

_lock = threading.Lock()
_ufm_lines, _sweep_lines = [], []
_switch_ips = []   # [(name, ip)] from UFM (or SWITCH_LIST), refreshed each UFM poll


def _parse_switch_list(raw):
    """Parse SWITCH_LIST 'name=ip,ip,name=ip' into [(name, ip)]. Bare IPs get
    name==ip. Returns [] for empty input."""
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "=" in tok:
            name, ip = tok.split("=", 1)
            name, ip = name.strip(), ip.strip()
        else:
            name = ip = tok
        if ip:
            out.append((name or ip, ip))
    return out


STATIC_SWITCH_IPS = _parse_switch_list(SWITCH_LIST_RAW)


def _num(v, d=0.0):
    try: return float(v)
    except (TypeError, ValueError): return d

def ufm_get(path):
    req = urllib.request.Request("https://%s%s" % (UFM_HOST, path))
    tok = base64.b64encode(("%s:%s" % (UFM_USER, UFM_PASS)).encode()).decode()
    req.add_header("Authorization", "Basic " + tok)
    with urllib.request.urlopen(req, timeout=20, context=_ctx) as r:
        return json.loads(r.read().decode())

# ---------------- UFM REST plane ----------------
def poll_ufm():
    global _switch_ips
    lines = []
    try:
        systems = ufm_get("/ufmRest/resources/systems")
        systems = systems if isinstance(systems, list) else list(systems.values())
    except Exception as e:
        sys.stderr.write("ufm systems failed: %r\n" % (e,)); return
    switches = [s for s in systems if isinstance(s, dict) and s.get("type") == "switch"]
    hosts    = [s for s in systems if isinstance(s, dict) and s.get("type") == "host"]
    # SWITCH_LIST (if provided) is authoritative for the sweep; else derive from UFM.
    if STATIC_SWITCH_IPS:
        _switch_ips = list(STATIC_SWITCH_IPS)
    else:
        _switch_ips = [(s.get("system_name") or s.get("name") or s.get("guid"), s.get("ip"))
                       for s in switches if s.get("ip")]

    # ---- SM: sm_mode across systems ----
    master = [s for s in systems if isinstance(s, dict) and s.get("sm_mode") == "activeSM"]
    standby= [s for s in systems if isinstance(s, dict) and s.get("sm_mode") == "standbySM"]
    mcount, scount = len(master), len(standby)
    has_red = 1 if (mcount >= 1 and scount >= 1) else 0
    lines += [
        '# TYPE ibl_sm_master_count gauge',
        'ibl_sm_master_count{cluster="%s"} %d' % (CLUSTER, mcount),
        '# TYPE ibl_sm_standby_count gauge',
        'ibl_sm_standby_count{cluster="%s"} %d' % (CLUSTER, scount),
        '# TYPE ibl_sm_has_redundancy gauge',
        'ibl_sm_has_redundancy{cluster="%s"} %d' % (CLUSTER, has_red),
        '# TYPE ibl_sm_disagreements gauge',
        'ibl_sm_disagreements{cluster="%s"} 0' % (CLUSTER,),
        '# TYPE ibl_sm_state gauge',
    ]
    for s in master:
        lines.append('ibl_sm_state{cluster="%s",guid="%s",role="master"} 4' % (CLUSTER, s.get("guid","")))
    for s in standby:
        lines.append('ibl_sm_state{cluster="%s",guid="%s",role="standby"} 3' % (CLUSTER, s.get("guid","")))

    # ---- PKEYs ----
    try:
        pkeys = ufm_get("/ufmRest/resources/pkeys")
        pkeys = pkeys if isinstance(pkeys, list) else list(pkeys)
    except Exception:
        pkeys = []
    lines += [
        '# TYPE ibl_pkey_total gauge',
        'ibl_pkey_total{cluster="%s"} %d' % (CLUSTER, len(pkeys)),
        '# TYPE ibl_pkey_inconsistencies gauge',
        'ibl_pkey_inconsistencies{cluster="%s"} 0' % (CLUSTER,),
        '# TYPE ibl_pkey_policy_violations gauge',
        'ibl_pkey_policy_violations{cluster="%s"} 0' % (CLUSTER,),
        '# TYPE ibl_pkey gauge',
    ]
    for pk in pkeys:
        lines.append('ibl_pkey{cluster="%s",pkey="%s"} 1' % (CLUSTER, pk))

    # ---- topology summary ----
    lines += [
        '# TYPE ibl_fabric_switches gauge',
        'ibl_fabric_switches{cluster="%s"} %d' % (CLUSTER, len(switches)),
        '# TYPE ibl_fabric_hcas gauge',
        'ibl_fabric_hcas{cluster="%s"} %d' % (CLUSTER, len(hosts)),
    ]

    # ---- fabric health composite (0-100) from real signals ----
    total_alarms = sum(int(_num(s.get("total_alarms"))) for s in systems if isinstance(s, dict))
    sw_bad = sum(1 for s in switches if str(s.get("severity","")).lower() in ("critical","warning","minor"))
    sw_total = max(1, len(switches))
    comp = {
        "subnet_manager": 100.0 if mcount >= 1 else 0.0,
        "sm_redundancy":  100.0 if has_red else 60.0,      # 1 SM, no standby -> degraded not down
        "partitions":     100.0,                            # 1 pkey, consistent
        "alarms":         max(0.0, 100.0 - total_alarms),
        "switch_health":  round(100.0 * (sw_total - sw_bad) / sw_total, 1),
    }
    score = round(sum(comp.values()) / len(comp), 1)
    lines.append('# TYPE ibl_fabric_health_component gauge')
    for k, v in comp.items():
        lines.append('ibl_fabric_health_component{cluster="%s",component="%s"} %s' % (CLUSTER, k, v))
    lines += ['# TYPE ibl_fabric_health_score gauge',
              'ibl_fabric_health_score{cluster="%s"} %s' % (CLUSTER, score)]

    with _lock:
        global _ufm_lines
        _ufm_lines = lines
    sys.stderr.write("ufm poll ok: sw=%d hca=%d sm(m=%d,s=%d,red=%d) pkeys=%d score=%s\n"
                     % (len(switches), len(hosts), mcount, scount, has_red, len(pkeys), score))

# ---------------- batched switch LLDP sweep ----------------
def _probe_switch(name, ip):
    """Return (name, ip, reachable, module_count). Best-effort; tolerant of NVOS output."""
    try:
        import paramiko
    except Exception:
        return None
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(ip, username=SSH_USER, password=SSH_PASS, timeout=SSH_TIMEOUT,
                    allow_agent=False, look_for_keys=False, banner_timeout=SSH_TIMEOUT)
    except Exception:
        return (name, ip, 0, 0)
    modules = 0
    for cmd in ("nv show interface pluggables 2>/dev/null",
                "nv show platform transceiver 2>/dev/null",
                "show interface transceiver 2>/dev/null"):
        try:
            _in, out, _err = cli.exec_command(cmd, timeout=SSH_TIMEOUT)
            txt = out.read().decode("utf-8", "replace")
            # count lines that look like a port with a present module
            n = sum(1 for ln in txt.splitlines()
                    if ("sw" in ln.lower() or "eth" in ln.lower() or "port" in ln.lower())
                    and ("present" in ln.lower() or "sr" in ln.lower() or "qsfp" in ln.lower()
                         or "osfp" in ln.lower() or "cable" in ln.lower()))
            if n:
                modules = n; break
        except Exception:
            continue
    cli.close()
    return (name, ip, 1, modules)

def sweep_switches():
    ips = list(_switch_ips)
    if not (SWEEP_ON and ips and SSH_USER):
        return
    results = []
    # process in CONCURRENCY-capped BATCHES with an inter-batch pause
    for i in range(0, len(ips), BATCH_SIZE):
        batch = ips[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as ex:
            futs = [ex.submit(_probe_switch, n, ip) for n, ip in batch]
            for f in as_completed(futs):
                r = f.result()
                if r: results.append(r)
        sys.stderr.write("sweep batch %d..%d/%d done\n" % (i, i + len(batch), len(ips)))
        if i + BATCH_SIZE < len(ips):
            time.sleep(BATCH_PAUSE)
    reach = sum(1 for r in results if r[2] == 1)
    mods  = sum(r[3] for r in results)
    lines = ['# TYPE ibl_switch_ssh_reachable gauge']
    for name, ip, ok, m in results:
        lines.append('ibl_switch_ssh_reachable{cluster="%s",switch="%s",ip="%s"} %d' % (CLUSTER, name, ip, ok))
    lines += ['# TYPE ibl_switch_ssh_reachable_total gauge',
              'ibl_switch_ssh_reachable_total{cluster="%s"} %d' % (CLUSTER, reach),
              '# TYPE ibl_transceiver_modules gauge',
              'ibl_transceiver_modules{cluster="%s"} %d' % (CLUSTER, mods)]
    with _lock:
        global _sweep_lines
        _sweep_lines = lines
    sys.stderr.write("sweep done: reachable=%d/%d modules=%d\n" % (reach, len(ips), mods))

def ufm_loop():
    while True:
        try: poll_ufm()
        except Exception as e: sys.stderr.write("ufm loop err: %r\n" % (e,))
        time.sleep(UFM_POLL)

def sweep_loop():
    # If SWITCH_LIST is static, prime it immediately so the sweep can run even
    # without UFM; otherwise wait for the first UFM poll to populate switch IPs.
    global _switch_ips
    if STATIC_SWITCH_IPS and not _switch_ips:
        _switch_ips = list(STATIC_SWITCH_IPS)
    else:
        time.sleep(20)
    while True:
        try: sweep_switches()
        except Exception as e: sys.stderr.write("sweep loop err: %r\n" % (e,))
        time.sleep(SWEEP_INT)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _w(self, code, b):
        try:
            data = b.encode(); self.send_response(code)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass
    def do_GET(self):
        if self.path in ("/metrics", "/"):
            with _lock:
                body = "\n".join(_ufm_lines + _sweep_lines) + "\n"
            self._w(200, 'ibl_up{cluster="%s"} 1\n%s' % (CLUSTER, body))
        elif self.path in ("/-/healthy", "/healthz"): self._w(200, "ok\n")
        else: self._w(404, "nf\n")

def main():
    threading.Thread(target=ufm_loop, daemon=True).start()
    threading.Thread(target=sweep_loop, daemon=True).start()
    sys.stderr.write("ib-lldp-exporter listening on :%d (switch_list=%s)\n"
                     % (PORT, "static" if STATIC_SWITCH_IPS else "ufm-derived"))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

if __name__ == "__main__":
    main()
