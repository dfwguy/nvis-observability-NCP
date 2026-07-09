#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install-node.sh — NVIS GPU-node observability agent installer (runs ON a node)
#
# Installs native systemd units (no container runtime required — DGX-OS / Ubuntu):
#   - dcgm-exporter   :9400  (connects to the local nv-hostengine on localhost:5555)
#   - node_exporter   :9100  (+ textfile collector directory for the .prom scripts)
#   - cgroup_exporter :9306  (per-Slurm-job cpu/mem/io from cgroup v2, keyed by jobid)
#   - grafana alloy   :12345 (scrapes 9400/9100/9306/12345, tails logs, pushes upstream)
#   - textfile collectors (optional): host_nvlink_collector + hca_verbs_collector,
#     run on a systemd timer, writing .prom into the node_exporter textfile dir.
#
# Binaries are staged on a cluster-shared dir ($SHARE, e.g. an NFS export visible
# to every node) and copied to local disk so services do not depend on the share
# at boot. See stage-binaries.sh to populate $SHARE (auto-detects amd64/arm64).
#
# Idempotent: re-running is safe; skips work when already onboarded (FORCE=1 to redo).
# Reversible: see uninstall-node.sh.
#
# ── Environment (all site values; NO hardcoded IPs/hostnames) ────────────────
#   METRICS_TARGET   ObsVM host/IP running Prometheus(:9090)+Loki(:3100). Required
#                    unless RELAY_HOST (or RW_URL/LOKI_PUSH_URL) is given.
#   RELAY_HOST       Head-node stream relay the (often network-blind) compute
#                    nodes push to. Defaults to METRICS_TARGET when nodes can
#                    reach the ObsVM directly. See head-node/ for the relay.
#   RELAY_RW_PORT    Relay port forwarded to ObsVM :9090   (default 19090)
#   RELAY_LOKI_PORT  Relay port forwarded to ObsVM :3100   (default 13100)
#   RW_URL           Full remote_write URL (overrides RELAY_HOST/port derivation)
#   LOKI_PUSH_URL    Full Loki push URL     (overrides RELAY_HOST/port derivation)
#   CLUSTER_NAME     Grafana `cluster` label                (default cluster)
#   DGX_FLAVOR       Hardware flavor label, e.g. dgx-superpod / h100 / gb200
#                    (default dgx-superpod)  → emitted as external label dgx_flavor
#   SHARE            Cluster-shared staging dir for binaries (default /cm/shared/nvis/bin)
#   TEXTFILE_DIR     node_exporter textfile collector dir
#                    (default /var/lib/node_exporter/textfile_collector)
#   COLLECTOR_INTERVAL   textfile collector cadence (default 30s)
#   ENABLE_TEXTFILE_COLLECTORS  1=install nvlink/hca .prom collectors (default 1)
#   FORCE            1 to force reinstall/restart (default 0)
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

# ── resolve targets from env (no site values baked in) ───────────────────────
METRICS_TARGET="${METRICS_TARGET:-}"
RELAY_HOST="${RELAY_HOST:-${METRICS_TARGET}}"
RELAY_RW_PORT="${RELAY_RW_PORT:-19090}"
RELAY_LOKI_PORT="${RELAY_LOKI_PORT:-13100}"
RW_URL="${RW_URL:-http://${RELAY_HOST}:${RELAY_RW_PORT}/api/v1/write}"
LOKI_PUSH_URL="${LOKI_PUSH_URL:-http://${RELAY_HOST}:${RELAY_LOKI_PORT}/loki/api/v1/push}"

CLUSTER_NAME="${CLUSTER_NAME:-cluster}"
DGX_FLAVOR="${DGX_FLAVOR:-dgx-superpod}"
SHARE="${SHARE:-/cm/shared/nvis/bin}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"
COLLECTOR_INTERVAL="${COLLECTOR_INTERVAL:-30s}"
ENABLE_TEXTFILE_COLLECTORS="${ENABLE_TEXTFILE_COLLECTORS:-1}"
FORCE="${FORCE:-0}"
HOST="$(hostname -s)"

log(){ echo "[${HOST}] $*"; }
fail(){ echo "[${HOST}] ERROR: $*" >&2; echo "NVIS_RESULT host=${HOST} verdict=FAIL reason=$*"; exit 1; }

# RELAY_HOST is empty only when neither RELAY_HOST nor METRICS_TARGET was set and
# no explicit RW_URL was provided — refuse rather than push to a broken URL.
if [[ -z "${RELAY_HOST}" && "${RW_URL}" == "http://:${RELAY_RW_PORT}/api/v1/write" ]]; then
  fail "no_target: set METRICS_TARGET (and RELAY_HOST for blind compute nodes) or RW_URL/LOKI_PUSH_URL"
fi

# ── idempotency fast-path ────────────────────────────────────────────────────
already_ok(){
  # NOTE: `systemctl is-active --quiet a b c` is unreliable across systemd
  # versions (some return 0 if ANY unit is active). Check each service on its
  # own so a single stopped agent (e.g. cgroup-exporter) is not masked.
  for svc in node_exporter dcgm-exporter cgroup-exporter alloy; do
    systemctl is-active --quiet "${svc}" 2>/dev/null || return 1
  done
  grep -qF "${RW_URL}" /etc/alloy/config.alloy 2>/dev/null || return 1
  grep -q "\"${HOST}\"" /etc/alloy/config.alloy 2>/dev/null || return 1
  # config must already scrape the cgroup exporter, else re-render to add it
  grep -q "localhost:9306" /etc/alloy/config.alloy 2>/dev/null || return 1
  return 0
}
if [[ "${FORCE}" != "1" ]] && already_ok; then
  log "ALREADY_ONBOARDED (node_exporter+dcgm-exporter+cgroup-exporter+alloy active, config → ${RW_URL}, instance=${HOST})"
  echo "NVIS_RESULT host=${HOST} node_exporter=OK dcgm=OK cgroup=OK alloy=OK ready=OK verdict=PASS state=preexisting"
  exit 0
fi

# ── sanity: staged binaries present ──────────────────────────────────────────
for b in alloy dcgm-exporter node_exporter cgroup_exporter dcgm-counters.csv; do
  [[ -f "${SHARE}/${b}" ]] || fail "missing_staged_${b} (checked ${SHARE}; run stage-binaries.sh)"
done

# ── install binaries (only if changed) ───────────────────────────────────────
install_bin(){ # src dst
  local src="$1" dst="$2"
  if [[ ! -f "${dst}" ]] || ! cmp -s "${src}" "${dst}"; then
    install -m0755 "${src}" "${dst}" || return 1
    log "installed ${dst}"
  fi
}
# NOTE: alloy → /usr/bin/alloy to match the common cluster staging/watchdog
# convention (avoids a two-binary fight). node/dcgm exporters → /usr/local/bin.
install_bin "${SHARE}/node_exporter"   /usr/local/bin/node_exporter   || fail "copy_node_exporter"
install_bin "${SHARE}/dcgm-exporter"   /usr/local/bin/dcgm-exporter   || fail "copy_dcgm_exporter"
install_bin "${SHARE}/cgroup_exporter" /usr/local/bin/cgroup_exporter || fail "copy_cgroup_exporter"
install_bin "${SHARE}/alloy"           /usr/bin/alloy                 || fail "copy_alloy"

mkdir -p /etc/dcgm-exporter /etc/alloy /var/lib/alloy/data "${TEXTFILE_DIR}"
install -m0644 "${SHARE}/dcgm-counters.csv" /etc/dcgm-exporter/counters.csv

# ── textfile collectors (optional): GPU NVLink + IB HCA verbs ────────────────
if [[ "${ENABLE_TEXTFILE_COLLECTORS}" == "1" ]]; then
  for c in host_nvlink_collector.sh hca_verbs_collector.sh; do
    if [[ -f "${SHARE}/${c}" ]]; then
      install -m0755 "${SHARE}/${c}" "/usr/local/bin/${c}"
    else
      log "WARN: ${c} not staged in ${SHARE} — skipping its timer"
    fi
  done
fi

# ── locate libdcgm (dcgm-exporter dlopens it at runtime) ─────────────────────
LIBDCGM_DIR="$(dirname "$(find /usr/lib /lib /cm/local/apps/cuda-dcgm -name 'libdcgm.so.4' 2>/dev/null | head -1)")"
if [[ -z "${LIBDCGM_DIR}" || "${LIBDCGM_DIR}" == "." ]]; then
  # arm64 (Grace) default; amd64 default is /usr/lib/x86_64-linux-gnu
  case "$(uname -m)" in
    aarch64) LIBDCGM_DIR="/usr/lib/aarch64-linux-gnu" ;;
    *)       LIBDCGM_DIR="/usr/lib/x86_64-linux-gnu" ;;
  esac
fi
log "libdcgm dir: ${LIBDCGM_DIR}"

# ── systemd unit: node_exporter (with textfile collector directory) ──────────
cat >/etc/systemd/system/node_exporter.service <<EOF
[Unit]
Description=Prometheus node_exporter (NVIS)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/local/bin/node_exporter \\
  --web.listen-address=:9100 \\
  --collector.textfile.directory=${TEXTFILE_DIR} \\
  --collector.cpu.info \\
  --collector.netdev \\
  --collector.netstat \\
  --collector.diskstats \\
  --collector.filesystem \\
  --collector.meminfo \\
  --collector.systemd
Restart=always
RestartSec=5
User=root
[Install]
WantedBy=multi-user.target
EOF

# ── systemd unit: dcgm-exporter (uses existing nv-hostengine @ localhost:5555) ─
cat >/etc/systemd/system/dcgm-exporter.service <<EOF
[Unit]
Description=NVIDIA DCGM Exporter (NVIS)
After=network-online.target nvidia-dcgm.service
Wants=network-online.target
[Service]
Environment=LD_LIBRARY_PATH=${LIBDCGM_DIR}
ExecStart=/usr/local/bin/dcgm-exporter --address=:9400 --remote-hostengine-info=localhost:5555 --collectors=/etc/dcgm-exporter/counters.csv
Restart=always
RestartSec=5
User=root
[Install]
WantedBy=multi-user.target
EOF

# ── systemd unit: cgroup_exporter (per-Slurm-job cgroup v2 metrics, keyed by jobid) ─
# --config.paths=/slurm auto-maps to /system.slice/slurmstepd.scope on cgroup v2.
# Emits cgroup_info{jobid,uid,username} + cgroup_cpu_*/cgroup_memory_* keyed by cgroup.
cat >/etc/systemd/system/cgroup-exporter.service <<EOF
[Unit]
Description=cgroup_exporter (NVIS — per-Slurm-job metrics)
After=network-online.target slurmd.service
Wants=network-online.target
[Service]
ExecStart=/usr/local/bin/cgroup_exporter --config.paths=/slurm --collect.proc --web.listen-address=:9306
Restart=always
RestartSec=5
User=root
[Install]
WantedBy=multi-user.target
EOF

# ── textfile collector timers (only for collectors that got staged) ──────────
declare -a COLLECTOR_UNITS=()
if [[ "${ENABLE_TEXTFILE_COLLECTORS}" == "1" ]]; then
  make_collector_timer(){ # unit-basename  /usr/local/bin/script  description
    local unit="$1" bin="$2" desc="$3"
    [[ -x "${bin}" ]] || return 0
    cat >/etc/systemd/system/${unit}.service <<EOF
[Unit]
Description=${desc}
After=node_exporter.service
[Service]
Type=oneshot
Environment=TEXTFILE_DIR=${TEXTFILE_DIR}
ExecStart=${bin}
EOF
    cat >/etc/systemd/system/${unit}.timer <<EOF
[Unit]
Description=Run ${desc} every ${COLLECTOR_INTERVAL}
[Timer]
OnBootSec=30s
OnUnitActiveSec=${COLLECTOR_INTERVAL}
AccuracySec=5s
[Install]
WantedBy=timers.target
EOF
    COLLECTOR_UNITS+=("${unit}.timer")
  }
  make_collector_timer nvis-nvlink-collector /usr/local/bin/host_nvlink_collector.sh "NVIS GPU NVLink textfile collector"
  make_collector_timer nvis-hca-collector    /usr/local/bin/hca_verbs_collector.sh   "NVIS IB HCA verbs textfile collector"
fi

# ── alloy River config (instance hardcoded to hostname; external cluster label) ─
cat >/etc/alloy/config.alloy <<EOF
// ─────────────────────────────────────────────────────────────────────────────
// NVIS GPU-node Alloy agent — instance=${HOST} cluster=${CLUSTER_NAME}
// Generated by install-node.sh. Do not edit by hand (re-run installer to update).
// ─────────────────────────────────────────────────────────────────────────────

logging {
  level  = "info"
  format = "logfmt"
}

// Prometheus remote_write → relay/ObsVM (WAL-buffered: survives receiver downtime)
prometheus.remote_write "obsvm" {
  endpoint {
    url = "${RW_URL}"
  }
  external_labels = {
    cluster    = "${CLUSTER_NAME}",
    dgx_flavor = "${DGX_FLAVOR}",
  }
}

// DCGM exporter (localhost:9400) — honor_timestamps=false per NVIS standard
prometheus.scrape "dcgm" {
  targets          = [{ __address__ = "localhost:9400" }]
  scrape_interval  = "15s"
  honor_timestamps = false
  forward_to       = [prometheus.relabel.dcgm.receiver]
}
prometheus.relabel "dcgm" {
  forward_to = [prometheus.remote_write.obsvm.receiver]
  rule {
    target_label = "job"
    replacement  = "dcgm-exporter"
  }
  rule {
    target_label = "instance"
    replacement  = "${HOST}"
  }
  rule {
    target_label = "node"
    replacement  = "${HOST}"
  }
}

// node_exporter (localhost:9100) — includes textfile .prom collectors
prometheus.scrape "node_exporter" {
  targets         = [{ __address__ = "localhost:9100" }]
  scrape_interval = "15s"
  forward_to      = [prometheus.relabel.node_exporter.receiver]
}
prometheus.relabel "node_exporter" {
  forward_to = [prometheus.remote_write.obsvm.receiver]
  rule {
    target_label = "job"
    replacement  = "node-exporter"
  }
  rule {
    target_label = "instance"
    replacement  = "${HOST}"
  }
  rule {
    target_label = "node"
    replacement  = "${HOST}"
  }
}

// cgroup_exporter (localhost:9306) — per-Slurm-job cpu/mem/io keyed by jobid
prometheus.scrape "cgroup_exporter" {
  targets         = [{ __address__ = "localhost:9306" }]
  scrape_interval = "15s"
  forward_to      = [prometheus.relabel.cgroup_exporter.receiver]
}
prometheus.relabel "cgroup_exporter" {
  forward_to = [prometheus.remote_write.obsvm.receiver]
  rule {
    target_label = "job"
    replacement  = "cgroup-exporter"
  }
  rule {
    target_label = "instance"
    replacement  = "${HOST}"
  }
  rule {
    target_label = "node"
    replacement  = "${HOST}"
  }
}

// Alloy self-metrics (localhost:12345) — per-node agent health visible centrally
prometheus.scrape "alloy" {
  targets         = [{ __address__ = "localhost:12345" }]
  scrape_interval = "15s"
  forward_to      = [prometheus.relabel.alloy.receiver]
}
prometheus.relabel "alloy" {
  forward_to = [prometheus.remote_write.obsvm.receiver]
  rule {
    target_label = "job"
    replacement  = "alloy"
  }
  rule {
    target_label = "instance"
    replacement  = "${HOST}"
  }
  rule {
    target_label = "node"
    replacement  = "${HOST}"
  }
}

// Loki write → relay/ObsVM
loki.write "obsvm" {
  endpoint {
    url = "${LOKI_PUSH_URL}"
  }
  external_labels = {
    cluster    = "${CLUSTER_NAME}",
    dgx_flavor = "${DGX_FLAVOR}",
    node       = "${HOST}",
  }
}

// Log files: syslog / kernel / DCGM host-engine / slurm
local.file_match "logfiles" {
  path_targets = [
    { __path__ = "/var/log/syslog",           job = "syslog", instance = "${HOST}" },
    { __path__ = "/var/log/kern.log",          job = "kernel", instance = "${HOST}" },
    { __path__ = "/var/log/nv-hostengine.log", job = "dcgm",   instance = "${HOST}" },
    { __path__ = "/var/log/slurm*",            job = "slurm",  instance = "${HOST}" },
  ]
}
loki.source.file "logfiles" {
  targets    = local.file_match.logfiles.targets
  forward_to = [loki.write.obsvm.receiver]
}

// systemd journal
loki.source.journal "journal" {
  forward_to    = [loki.write.obsvm.receiver]
  relabel_rules = loki.relabel.journal.rules
  labels = {
    job      = "journal",
    instance = "${HOST}",
  }
}
loki.relabel "journal" {
  forward_to = []
  rule {
    source_labels = ["__journal__systemd_unit"]
    target_label  = "unit"
  }
  rule {
    source_labels = ["__journal_priority_keyword"]
    target_label  = "level"
  }
}
EOF

# ── systemd unit: alloy ──────────────────────────────────────────────────────
cat >/etc/systemd/system/alloy.service <<EOF
[Unit]
Description=Grafana Alloy (NVIS)
After=network-online.target
Wants=network-online.target
[Service]
Environment=HOSTNAME=${HOST}
Environment=NVIS_CLUSTER=${CLUSTER_NAME}
ExecStart=/usr/bin/alloy run /etc/alloy/config.alloy --storage.path=/var/lib/alloy/data --server.http.listen-addr=127.0.0.1:12345
Restart=always
RestartSec=5
User=root
[Install]
WantedBy=multi-user.target
EOF

# ── enable + (re)start ───────────────────────────────────────────────────────
systemctl daemon-reload
systemctl reset-failed node_exporter dcgm-exporter cgroup-exporter alloy 2>/dev/null || true
for svc in node_exporter dcgm-exporter cgroup-exporter alloy; do
  systemctl enable "${svc}" >/dev/null 2>&1
  systemctl restart "${svc}"
done
for t in "${COLLECTOR_UNITS[@]:-}"; do
  [[ -z "${t}" ]] && continue
  systemctl enable "${t}" >/dev/null 2>&1
  systemctl restart "${t}" 2>/dev/null || true
done

# ── validate node-local endpoints (retry — services take a few s to warm up) ──
# NOTE: pipefail off here — `curl | grep -q` matches early on large payloads and
# SIGPIPEs curl (exit 141), which pipefail would wrongly treat as a check failure.
set +o pipefail
ne_ok=FAIL; dcgm_ok=FAIL; cge_ok=FAIL; alloy_ok=FAIL; ready_ok=FAIL
for _try in 1 2 3 4 5 6; do
  [[ "${ne_ok}"    == OK ]] || { curl -sf --max-time 5 http://localhost:9100/metrics  2>/dev/null | grep -q '^node_cpu' && ne_ok=OK; }
  [[ "${dcgm_ok}"  == OK ]] || { curl -sf --max-time 8 http://localhost:9400/metrics  2>/dev/null | grep -q 'DCGM_FI'   && dcgm_ok=OK; }
  # cgroup_exporter: build_info is always present; cgroup_* job series appear only when jobs run
  [[ "${cge_ok}"   == OK ]] || { curl -sf --max-time 5 http://localhost:9306/metrics  2>/dev/null | grep -qE '^(cgroup_exporter_build_info|cgroup_info)' && cge_ok=OK; }
  [[ "${alloy_ok}" == OK ]] || { curl -sf --max-time 5 http://localhost:12345/metrics 2>/dev/null | grep -qE '^(alloy_build_info|go_goroutines)' && alloy_ok=OK; }
  [[ "${ready_ok}" == OK ]] || { curl -sf --max-time 5 http://localhost:12345/-/ready 2>/dev/null | grep -qi ready && ready_ok=OK; }
  [[ "${ne_ok}" == OK && "${dcgm_ok}" == OK && "${cge_ok}" == OK && "${alloy_ok}" == OK && "${ready_ok}" == OK ]] && break
  sleep 5
done

verdict=PASS
[[ "${ne_ok}" == OK && "${dcgm_ok}" == OK && "${cge_ok}" == OK && "${alloy_ok}" == OK && "${ready_ok}" == OK ]] || verdict=FAIL

log "node_exporter=${ne_ok} dcgm=${dcgm_ok} cgroup=${cge_ok} alloy=${alloy_ok} ready=${ready_ok}"
if [[ "${verdict}" == FAIL ]]; then
  log "--- diag ---"
  systemctl is-active node_exporter dcgm-exporter cgroup-exporter alloy 2>&1 | tr '\n' ' '; echo
  journalctl -u dcgm-exporter -u cgroup-exporter -u alloy --no-pager -n 15 2>/dev/null
fi
echo "NVIS_RESULT host=${HOST} node_exporter=${ne_ok} dcgm=${dcgm_ok} cgroup=${cge_ok} alloy=${alloy_ok} ready=${ready_ok} verdict=${verdict} state=installed"
[[ "${verdict}" == PASS ]] && exit 0 || exit 1
