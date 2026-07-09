#!/usr/bin/env bash
# =============================================================================
# host_nvlink_collector.sh — per-NODE capture of GPU-side NVLink + fabric health,
# emitted as Prometheus node_exporter TEXTFILE format so the platform scrapes it
# with ZERO new exporter. Complements switch-side IB telemetry with the GPU-side
# view (per-link CRC/replay/recovery errors, link state, fabric-manager, IMEX)
# that the fabric counters cannot see.
#
# Deploy: install-node.sh copies this to /usr/local/bin and runs it on a systemd
# timer (default every 30s), writing into the node_exporter textfile dir. To run
# it by hand across the fleet you can use any parallel-ssh tool, e.g.:
#   pdsh -w "$NODE_LIST" 'TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector \
#         /usr/local/bin/host_nvlink_collector.sh'
# node_exporter must run with:
#   --collector.textfile.directory=$TEXTFILE_DIR
# then su1_nvlink.prom is scraped automatically, labeled by the node it ran on.
#
# NOTE: metric names keep the su1_ prefix for dashboard compatibility across the
# platform; they are per-node GPU NVLink metrics with no site-specific meaning.
#
# Run as root (nvidia-smi nvlink / fabricmanager need privilege).
# =============================================================================
set -uo pipefail
OUT=${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}/su1_nvlink.prom
TMP="${OUT}.$$"
mkdir -p "$(dirname "$OUT")" 2>/dev/null || true
: > "$TMP"
trap 'rm -f "$TMP"' EXIT

emit(){ echo "$1" >> "$TMP"; }
emit "# HELP su1_nvlink_error_total GPU-side NVLink per-link error counters (nvidia-smi nvlink -e)"
emit "# TYPE su1_nvlink_error_total gauge"

# ---- per-GPU per-link NVLink error counters ---------------------------------
# nvidia-smi nvlink -e prints CRC/replay/recovery counts per GPU per link.
nvidia-smi nvlink -e 2>/dev/null | awk '
  /^GPU/ { gsub(/[:]/,""); gpu=$2; next }
  /Link [0-9]+:/ {
    # capture "Link N:" then the metric name/value on same or context lines
    match($0, /Link ([0-9]+):[ \t]*(.*)/, m); link=m[1]; rest=m[2];
    # rest like "Replay Errors: 0" or a header; handle "<name>: <val>"
    if (match(rest, /(.+):[ \t]*([0-9]+)/, kv)) {
      name=kv[1]; val=kv[2];
      gsub(/[^A-Za-z0-9]+/,"_",name); name=tolower(name);
      printf "su1_nvlink_error_total{gpu=\"%s\",link=\"%s\",kind=\"%s\"} %s\n", gpu, link, name, val;
    }
  }' >> "$TMP"

# ---- per-GPU per-link NVLink state + speed (nvidia-smi nvlink -s) ------------
emit "# HELP su1_nvlink_up NVLink per-link up(1)/down(0)"
emit "# TYPE su1_nvlink_up gauge"
emit "# HELP su1_nvlink_speed_gbps NVLink per-link speed GB/s"
emit "# TYPE su1_nvlink_speed_gbps gauge"
nvidia-smi nvlink -s 2>/dev/null | awk '
  /^GPU/ { gsub(/[:]/,""); gpu=$2; next }
  /Link [0-9]+:/ {
    match($0, /Link ([0-9]+):[ \t]*(.*)/, m); link=m[1]; info=m[2];
    up = (info ~ /<inactive>|Disabled|down/i) ? 0 : 1;
    printf "su1_nvlink_up{gpu=\"%s\",link=\"%s\"} %d\n", gpu, link, up;
    if (match(info, /([0-9.]+) GB\/s/, s)) printf "su1_nvlink_speed_gbps{gpu=\"%s\",link=\"%s\"} %s\n", gpu, link, s[1];
  }' >> "$TMP"

# ---- Fabric Manager + IMEX service health -----------------------------------
# A failed nvidia-fabricmanager / nvidia-imex leaves GPUs unable to register on
# the NVLink fabric — a common cause of a node that is "up" but cannot run.
emit "# HELP su1_service_active systemd service active(1)/failed(0)"
emit "# TYPE su1_service_active gauge"
for svc in nvidia-fabricmanager nvidia-imex; do
  st=$(systemctl is-active "$svc" 2>/dev/null)
  v=0; [ "$st" = "active" ] && v=1
  emit "su1_service_active{service=\"$svc\"} $v"
done

# ---- GPU fabric/clique state (nvidia-smi -q fabric) -------------------------
emit "# HELP su1_gpu_fabric_state GPU fabric registration state (1=completed)"
emit "# TYPE su1_gpu_fabric_state gauge"
nvidia-smi -q 2>/dev/null | awk '
  /GPU 0000/ { gpu=$0 }
  /Fabric/ {infab=1}
  /State/ && infab { st=($NF ~ /Completed|COMPLETED/)?1:0; printf "su1_gpu_fabric_state{gpu=\"%s\"} %d\n", gpu, st; infab=0 }' >> "$TMP" 2>/dev/null

mv -f "$TMP" "$OUT"
trap - EXIT
