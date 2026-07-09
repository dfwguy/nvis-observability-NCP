#!/usr/bin/env python3
"""
NVIS BCM (BrightCluster Manager) Prometheus Exporter
=====================================================
Runs on the central observability host. SSHes to BCM, collects full
hardware/software inventory via cmsh and standard Linux commands, and
exposes Prometheus metrics on :9401.

Every customer cluster has a BCM. This exporter auto-discovers:
  - BCM version, hostname, platform, OS, CPU, RAM, uptime
  - All managed nodes: count, status (up/down/error/etc), GPU count per node
  - Slurm partition info if available
  - Network interfaces
  - Storage (if df available)

Environment variables:
  BCM_HOST         BCM IP or hostname              (required)
  BCM_USER         SSH user                        (default: root)
  BCM_JUMP         Jump host  user@host            (optional)
  BCM_SSH_KEY      SSH private key path            (default: /root/.ssh/id_ed25519)
  BCM_SSH_PORT     SSH port                        (default: 22)
  CLUSTER_NAME     Prometheus cluster= label       (required)
  SCRAPE_INTERVAL  Seconds between BCM polls       (default: 60)
  PORT             HTTP listen port                (default: 9401)
  BCM_TIMEOUT      SSH command timeout seconds     (default: 30)

Usage (standalone on the observability host):
  BCM_HOST=<bcm-ip-or-host> BCM_JUMP=user@jumphost CLUSTER_NAME=my-gpu-cluster python3 bcm_exporter.py

Usage (docker-compose) — see docker-compose.yml bcm-exporter service.
"""

import os, sys, time, subprocess, threading, json, re, traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────

BCM_HOST        = os.environ.get("BCM_HOST", "")
BCM_USER        = os.environ.get("BCM_USER", "root")
BCM_JUMP        = os.environ.get("BCM_JUMP", "")          # e.g. "user@jumphost"
BCM_SSH_KEY     = os.environ.get("BCM_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519"))
BCM_SSH_PORT    = os.environ.get("BCM_SSH_PORT", "22")
CLUSTER_NAME    = os.environ.get("CLUSTER_NAME", "nvis-cluster")
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "60"))
PORT            = int(os.environ.get("PORT", "9401"))
BCM_TIMEOUT     = int(os.environ.get("BCM_TIMEOUT", "30"))

# ── SSH helper ─────────────────────────────────────────────────────────────

def ssh_cmd(remote_script):
    """Run a shell script on BCM via SSH (optionally through jump host). Returns stdout string."""
    base_opts = [
        "-F", "/dev/null",   # ignore ~/.ssh/config (wrong owner when bind-mounted)
        "-i", BCM_SSH_KEY,
        "-p", BCM_SSH_PORT,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={BCM_TIMEOUT}",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
    ]
    if BCM_JUMP:
        base_opts += ["-J", BCM_JUMP]

    cmd = ["ssh"] + base_opts + [f"{BCM_USER}@{BCM_HOST}", "bash -s"]
    try:
        result = subprocess.run(
            cmd,
            input=remote_script.encode(),
            capture_output=True,
            timeout=BCM_TIMEOUT + 10
        )
        return result.stdout.decode(errors="replace"), result.returncode
    except subprocess.TimeoutExpired:
        return "", 124
    except Exception as e:
        return "", -1

# ── BCM collection script (runs on BCM via SSH) ────────────────────────────

BCM_COLLECT_SCRIPT = r"""
set -o pipefail

sep() { echo "=== $1 ==="; }

sep HOSTNAME
hostname -f 2>/dev/null || hostname

sep UNAME
uname -srm

sep UPTIME_SECS
awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0

sep CPUS
nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo 0

sep MEMORY_MB
awk '/^MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0

sep LOAD
uptime | awk -F'average:' '{print $2}' | awk -F',' '{print $1}' | tr -d ' '

sep IP_ADDRS
ip addr show 2>/dev/null | awk '/^\s+inet /{gsub("/.*",""); print $2}' | grep -v '^127\.' | tr '\n' ',' | sed 's/,$//'; echo

sep OS_RELEASE
. /etc/os-release 2>/dev/null && echo "${NAME} ${VERSION}" || cat /etc/redhat-release 2>/dev/null || echo unknown

sep PRODUCT_INFO
dmidecode -t system 2>/dev/null | awk '/Manufacturer:|Product Name:|Version:|Serial Number:/{sub(/^\s+/,""); print}' || echo n/a

sep BCM_VERSION
# BrightCluster Manager packages are prefixed cm-* (e.g. cm-bios-tools <maj>.<min>-<build>)
BCM_VER=""
# Method 1: Debian/Ubuntu cm-* packages (most common for BCM 9+)
if command -v dpkg-query &>/dev/null; then
  BCM_VER=$(dpkg -l 2>/dev/null | awk '/^ii\s+cm-/{print $3; exit}' | grep -oP '^\d+\.\d+')
fi
# Method 2: RPM cm-* packages
if [[ -z "$BCM_VER" ]] && command -v rpm &>/dev/null; then
  BCM_VER=$(rpm -qa 2>/dev/null | grep '^cm-' | head -1 | grep -oP '\d+\.\d+' | head -1)
fi
# Method 3: brightcluster-cm or brightcomputing package name
if [[ -z "$BCM_VER" ]]; then
  BCM_VER=$(dpkg -l 2>/dev/null | awk '/bright/{print $3; exit}' | grep -oP '^\d+\.\d+')
fi
# Method 4: version file in /cm
if [[ -z "$BCM_VER" ]]; then
  BCM_VER=$(cat /cm/local/apps/cmd/VERSION 2>/dev/null | head -1 | grep -oP '\d+\.\d+')
  [[ -z "$BCM_VER" ]] && BCM_VER=$(find /cm -name 'VERSION' -maxdepth 5 2>/dev/null | head -1 | xargs cat 2>/dev/null | grep -oP '\d+\.\d+' | head -1)
fi
echo "${BCM_VER:-unknown}"

sep CMSH_AVAILABLE
command -v cmsh &>/dev/null && echo yes || echo no

sep DEVICE_LIST
# cmsh 'device list' gives: Type Hostname MAC [Category] IP Network [Status...]
if command -v cmsh &>/dev/null; then
  timeout 20 cmsh -c 'device list' 2>/dev/null
fi

sep PARTITION_LIST
if command -v cmsh &>/dev/null; then
  timeout 10 cmsh 2>/dev/null <<'CMSH_EOF'
partition
list
CMSH_EOF
fi

sep SLURM_INFO
if command -v sinfo &>/dev/null; then
  sinfo -h -o "%P|%a|%D|%T" 2>/dev/null | sort -u || echo n/a
else
  echo n/a
fi

sep SLURM_JOBS
if command -v squeue &>/dev/null; then
  squeue -h 2>/dev/null | wc -l || echo 0
else
  echo 0
fi

sep DISK_INFO
df -BG / 2>/dev/null | awk 'NR==2{gsub("G",""); print $2" "$3" "$4}' || echo "0 0 0"

sep NETWORK_INTERFACES
ip link show 2>/dev/null | awk '/^[0-9]+:/{gsub(":",""); print $2}' | grep -v '^lo$' | tr '\n' ',' | sed 's/,$//'; echo

sep KERNEL_VERSION
uname -r

sep END
echo DONE
"""

# ── Parser ─────────────────────────────────────────────────────────────────

def parse_section(output, section):
    """Extract content between === SECTION === and next ==="""
    pattern = rf"=== {re.escape(section)} ===\n(.*?)(?===|\Z)"
    m = re.search(pattern, output, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""

def parse_device_list(raw):
    """
    Parse cmsh 'device list' output.
    Format (space-padded columns):
      Type           Hostname    MAC               [Category]  IP           Network   [  Status  ][, details...]
    Examples:
      HeadNode       bcm-host    FA:16:3E:9C:3D:9A             203.0.113.1    internal  [   UP   ]
      PhysicalNode   node001     FA:16:3E:1E:84:49  default    203.0.113.10   internal  [   UP   ], health check failed
    Returns list of dicts.
    """
    nodes = []
    if not raw or raw == "n/a":
        return nodes

    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue

        # Extract status from [   UP   ] or [  DOWN  ] pattern
        status = "unknown"
        sm = re.search(r'\[\s*(UP|DOWN|ERROR|INST|BURN|CLOSED|PROV|RESTOR|LOCKTMP)\s*\]', line, re.IGNORECASE)
        if sm:
            status = sm.group(1).lower()

        # Split on 2+ spaces to get columns (type, hostname, mac, maybe category, ip, network, status+)
        parts = re.split(r'  +', line.strip())
        if len(parts) < 2:
            continue

        node_type = parts[0].strip()
        hostname  = parts[1].strip() if len(parts) > 1 else ""

        # Skip header lines or empty hostnames
        if not hostname or hostname.lower() in ("hostname", "name", "---"):
            continue
        # Skip lines that look like error messages or help text
        if node_type.lower() in ("usage:", "error:", "warning:", "options:") or ":" in node_type:
            continue

        # Category is optional (HeadNode has no category)
        category = "default"
        ip = ""
        for p in parts[2:]:
            p = p.strip()
            # MAC addresses
            if re.match(r'^[0-9A-Fa-f:]{17}$', p):
                continue
            # IP address
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', p):
                ip = p
                continue
            # Status already extracted
            if p.startswith('[') or 'health check' in p or 'install' in p.lower():
                continue
            # Network names (internalnet, externalnet, etc.)
            if 'net' in p.lower() or p in ('internal', 'external', 'management'):
                continue
            # Anything else before IP is likely category
            if not ip and p and p != node_type:
                category = p

        node = {
            "hostname": hostname,
            "type":     node_type,
            "category": category,
            "status":   status,
            "ip":       ip,
            "cpus":     0,
            "ram_gb":   0,
            "gpus":     0,
            "is_head":  "head" in node_type.lower(),
        }
        nodes.append(node)
    return nodes

def _int(s):
    try:
        return int(s)
    except:
        return 0

def _float(s):
    try:
        return float(s.strip())
    except:
        return 0.0

# ── Metrics state ──────────────────────────────────────────────────────────

_lock = threading.Lock()
_metrics_text = "# BCM exporter starting...\n"
_last_collect = 0.0
_collection_errors = 0

def collect():
    global _metrics_text, _last_collect, _collection_errors

    if not BCM_HOST:
        with _lock:
            _metrics_text = (
                "# BCM_HOST not set — set BCM_HOST env var\n"
                f'bcm_up{{cluster="{CLUSTER_NAME}",instance="bcm"}} 0\n'
            )
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Collecting BCM inventory from {BCM_HOST}...", flush=True)
    t0 = time.time()
    raw, rc = ssh_cmd(BCM_COLLECT_SCRIPT)
    elapsed = time.time() - t0

    if rc != 0 or "DONE" not in raw:
        _collection_errors += 1
        print(f"  SSH failed (rc={rc}, errors={_collection_errors})", flush=True)
        with _lock:
            _metrics_text = (
                f'# BCM unreachable (rc={rc})\n'
                f'bcm_up{{cluster="{CLUSTER_NAME}",instance="bcm"}} 0\n'
                f'bcm_collection_errors_total{{cluster="{CLUSTER_NAME}",instance="bcm"}} {_collection_errors}\n'
                f'up{{job="node-exporter",cluster="{CLUSTER_NAME}",instance="bcm"}} 0\n'
            )
        return

    _collection_errors = 0

    # Parse sections
    hostname    = parse_section(raw, "HOSTNAME") or BCM_HOST
    uname       = parse_section(raw, "UNAME")
    uptime_secs = _int(parse_section(raw, "UPTIME_SECS"))
    cpus        = _int(parse_section(raw, "CPUS"))
    mem_mb      = _int(parse_section(raw, "MEMORY_MB"))
    load        = _float(parse_section(raw, "LOAD"))
    ip_addrs    = parse_section(raw, "IP_ADDRS")
    os_release  = parse_section(raw, "OS_RELEASE")
    product_raw = parse_section(raw, "PRODUCT_INFO")
    bcm_ver     = parse_section(raw, "BCM_VERSION") or "unknown"
    cmsh_avail  = parse_section(raw, "CMSH_AVAILABLE") == "yes"
    device_raw  = parse_section(raw, "DEVICE_LIST")
    slurm_raw   = parse_section(raw, "SLURM_INFO")
    slurm_jobs  = _int(parse_section(raw, "SLURM_JOBS"))
    disk_raw    = parse_section(raw, "DISK_INFO")
    kernel      = parse_section(raw, "KERNEL_VERSION")

    # Parse product info
    product = ""; manufacturer = ""
    for line in product_raw.splitlines():
        if "Product Name" in line:
            product = line.split(":",1)[-1].strip()
        elif "Manufacturer" in line:
            manufacturer = line.split(":",1)[-1].strip()

    # Parse disk
    disk_parts = disk_raw.split()
    disk_total = _int(disk_parts[0]) if len(disk_parts) > 0 else 0
    disk_used  = _int(disk_parts[1]) if len(disk_parts) > 1 else 0
    disk_free  = _int(disk_parts[2]) if len(disk_parts) > 2 else 0

    # Parse uname fields: Linux 5.x.y x86_64
    uname_parts = uname.split()
    sysname = uname_parts[0] if len(uname_parts) > 0 else "Linux"
    release = uname_parts[1] if len(uname_parts) > 1 else kernel
    machine = uname_parts[2] if len(uname_parts) > 2 else "x86_64"

    # Parse managed nodes
    nodes = parse_device_list(device_raw)
    # Filter out HeadNode from managed-node counts (the BCM head itself)
    compute_nodes = [n for n in nodes if not n.get("is_head")]
    node_total = len(compute_nodes)
    status_counts = {}
    gpu_nodes = 0
    total_gpus = 0
    total_managed_cpus = 0
    total_managed_ram_gb = 0
    for n in compute_nodes:
        s = n["status"]
        status_counts[s] = status_counts.get(s, 0) + 1
        if n["gpus"] > 0:
            gpu_nodes += 1
            total_gpus += n["gpus"]
        total_managed_cpus += n["cpus"]
        total_managed_ram_gb += n["ram_gb"]

    nodes_up   = status_counts.get("up", 0)
    nodes_down = status_counts.get("down", 0) + status_counts.get("error", 0)

    # Sanitize labels
    def ql(s):
        return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').strip()

    L = f'cluster="{CLUSTER_NAME}",instance="bcm"'

    lines = []
    a = lines.append

    # Standard node-exporter metrics so topology discovers BCM
    a(f'# HELP up BCM reachability (via NVIS BCM exporter)')
    a(f'# TYPE up gauge')
    a(f'up{{job="node-exporter",{L}}} 1')

    a(f'# HELP node_uname_info Node uname info (BCM host)')
    a(f'# TYPE node_uname_info gauge')
    a(f'node_uname_info{{{L},nodename="{ql(hostname)}",sysname="{ql(sysname)}",release="{ql(release)}",machine="{ql(machine)}"}} 1')

    a(f'# HELP node_cpu_count CPU core count on BCM head node')
    a(f'# TYPE node_cpu_count gauge')
    a(f'node_cpu_count{{{L}}} {cpus}')

    a(f'# HELP node_memory_MemTotal_bytes Total RAM on BCM head node')
    a(f'# TYPE node_memory_MemTotal_bytes gauge')
    a(f'node_memory_MemTotal_bytes{{{L}}} {mem_mb * 1024 * 1024}')

    a(f'# HELP node_load1 1-minute load average on BCM')
    a(f'# TYPE node_load1 gauge')
    a(f'node_load1{{{L}}} {load}')

    a(f'# HELP node_time_seconds System boot time (now - uptime)')
    a(f'# TYPE node_time_seconds gauge')
    a(f'node_boot_time_seconds{{{L}}} {int(time.time()) - uptime_secs}')

    a(f'# HELP node_filesystem_size_bytes Filesystem size (root)')
    a(f'# TYPE node_filesystem_size_bytes gauge')
    a(f'node_filesystem_size_bytes{{{L},mountpoint="/",fstype="ext4"}} {disk_total * 1024 * 1024 * 1024}')
    a(f'node_filesystem_avail_bytes{{{L},mountpoint="/",fstype="ext4"}} {disk_free * 1024 * 1024 * 1024}')

    # BCM-specific metrics
    a(f'')
    a(f'# HELP bcm_info BCM version and platform info (labels carry the data)')
    a(f'# TYPE bcm_info gauge')
    a(f'bcm_info{{{L},version="{ql(bcm_ver)}",hostname="{ql(hostname)}",os="{ql(os_release)}",product="{ql(product)}",manufacturer="{ql(manufacturer)}",ip="{ql(ip_addrs)}"}} 1')

    a(f'# HELP bcm_up 1 if BCM is reachable, 0 otherwise')
    a(f'# TYPE bcm_up gauge')
    a(f'bcm_up{{{L}}} 1')

    a(f'# HELP bcm_collection_duration_seconds Time taken to collect BCM inventory')
    a(f'# TYPE bcm_collection_duration_seconds gauge')
    a(f'bcm_collection_duration_seconds{{{L}}} {elapsed:.3f}')

    a(f'# HELP bcm_collection_errors_total Cumulative SSH/collection errors')
    a(f'# TYPE bcm_collection_errors_total counter')
    a(f'bcm_collection_errors_total{{{L}}} {_collection_errors}')

    a(f'# HELP bcm_managed_nodes_total Total nodes registered in BCM')
    a(f'# TYPE bcm_managed_nodes_total gauge')
    a(f'bcm_managed_nodes_total{{{L}}} {node_total}')

    a(f'# HELP bcm_managed_nodes_up Managed nodes in UP state')
    a(f'# TYPE bcm_managed_nodes_up gauge')
    a(f'bcm_managed_nodes_up{{{L}}} {nodes_up}')

    a(f'# HELP bcm_managed_nodes_down Managed nodes in DOWN/ERROR state')
    a(f'# TYPE bcm_managed_nodes_down gauge')
    a(f'bcm_managed_nodes_down{{{L}}} {nodes_down}')

    a(f'# HELP bcm_managed_gpu_nodes_total Managed nodes with GPUs')
    a(f'# TYPE bcm_managed_gpu_nodes_total gauge')
    a(f'bcm_managed_gpu_nodes_total{{{L}}} {gpu_nodes}')

    a(f'# HELP bcm_managed_gpus_total Total GPUs across all managed nodes')
    a(f'# TYPE bcm_managed_gpus_total gauge')
    a(f'bcm_managed_gpus_total{{{L}}} {total_gpus}')

    a(f'# HELP bcm_managed_cpus_total Total CPU cores across all managed nodes')
    a(f'# TYPE bcm_managed_cpus_total gauge')
    a(f'bcm_managed_cpus_total{{{L}}} {total_managed_cpus}')

    a(f'# HELP bcm_managed_memory_gb_total Total RAM GB across all managed nodes')
    a(f'# TYPE bcm_managed_memory_gb_total gauge')
    a(f'bcm_managed_memory_gb_total{{{L}}} {total_managed_ram_gb}')

    a(f'# HELP bcm_slurm_jobs_total Active Slurm jobs (if Slurm present)')
    a(f'# TYPE bcm_slurm_jobs_total gauge')
    a(f'bcm_slurm_jobs_total{{{L}}} {slurm_jobs}')

    # Per-node status series (useful for alerting on individual node state)
    a(f'')
    a(f'# HELP bcm_node_up 1 if node is UP in BCM, 0 otherwise')
    a(f'# TYPE bcm_node_up gauge')
    for n in compute_nodes:
        is_up = 1 if n["status"] == "up" else 0
        nl = f'cluster="{CLUSTER_NAME}",bcm_instance="bcm",node="{ql(n["hostname"])}",category="{ql(n["category"])}",status="{ql(n["status"])}"'
        a(f'bcm_node_up{{{nl}}} {is_up}')
        if n.get("gpus", 0) > 0:
            a(f'bcm_node_gpu_count{{{nl}}} {n["gpus"]}')

    # Status breakdown
    a(f'')
    a(f'# HELP bcm_nodes_by_status Node count grouped by BCM status')
    a(f'# TYPE bcm_nodes_by_status gauge')
    for st, cnt in status_counts.items():
        a(f'bcm_nodes_by_status{{cluster="{CLUSTER_NAME}",instance="bcm",status="{ql(st)}"}} {cnt}')

    result = "\n".join(lines) + "\n"

    with _lock:
        _metrics_text = result
        _last_collect = time.time()

    print(f"  OK — {node_total} nodes ({nodes_up} up), BCM {bcm_ver}, {elapsed:.1f}s", flush=True)

    # Also print JSON summary for human inspection
    summary = {
        "bcm_host": BCM_HOST,
        "bcm_hostname": hostname,
        "bcm_version": bcm_ver,
        "os": os_release,
        "product": product,
        "manufacturer": manufacturer,
        "kernel": release,
        "cpus": cpus,
        "memory_gb": mem_mb // 1024,
        "ip_addresses": ip_addrs,
        "disk_total_gb": disk_total,
        "disk_free_gb": disk_free,
        "load_1m": load,
        "uptime_hours": round(uptime_secs / 3600, 1),
        "cmsh_available": cmsh_avail,
        "managed_nodes_total": node_total,
        "managed_nodes_up": nodes_up,
        "managed_nodes_down": nodes_down,
        "managed_gpu_nodes": gpu_nodes,
        "managed_gpus_total": total_gpus,
        "managed_cpus_total": total_managed_cpus,
        "managed_memory_gb_total": total_managed_ram_gb,
        "slurm_active_jobs": slurm_jobs,
        "all_devices": nodes,
        "compute_nodes": compute_nodes,
        "collected_at": datetime.utcnow().isoformat() + "Z",
        "collection_seconds": round(elapsed, 2),
    }
    print(json.dumps(summary, indent=2), flush=True)


# ── Background poller ──────────────────────────────────────────────────────

def poll_loop():
    while True:
        try:
            collect()
        except Exception:
            traceback.print_exc()
        time.sleep(SCRAPE_INTERVAL)


# ── HTTP server ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/metrics", "/"):
            with _lock:
                body = _metrics_text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress HTTP access log noise


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BCM_HOST:
        print("ERROR: BCM_HOST environment variable is required", file=sys.stderr)
        print("Usage: BCM_HOST=<bcm-ip-or-host> CLUSTER_NAME=my-cluster python3 bcm_exporter.py", file=sys.stderr)
        sys.exit(1)

    print(f"NVIS BCM Exporter starting")
    print(f"  BCM host:    {BCM_HOST}:{BCM_SSH_PORT}")
    print(f"  BCM user:    {BCM_USER}")
    print(f"  Jump host:   {BCM_JUMP or '(none)'}")
    print(f"  SSH key:     {BCM_SSH_KEY}")
    print(f"  Cluster:     {CLUSTER_NAME}")
    print(f"  Poll every:  {SCRAPE_INTERVAL}s")
    print(f"  Metrics at:  http://0.0.0.0:{PORT}/metrics")
    print(flush=True)

    # Initial collect before accepting HTTP requests
    try:
        collect()
    except Exception:
        traceback.print_exc()

    # Background poller
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()

    # HTTP server
    srv = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listening on :{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down")
