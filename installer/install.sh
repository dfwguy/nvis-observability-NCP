#!/usr/bin/env bash
# =============================================================================
# NVIS Observability Appliance — interactive interview installer
# Version: 1.0.0
#
# Run this ON the observability host, from the repo root or from installer/:
#
#   ./installer/install.sh
#
# It interviews you for your site values (cluster name, Grafana password, and
# the optional integrations: BMC/Redfish, BCM, UFM/IB fabric, NetQ, Slurm job
# monitor, NVLMapper, node agents), writes the gitignored .env files from the
# committed *.example templates, then `docker compose up -d` the stacks you
# enabled.
#
# Ground rules (same as the rest of this repo):
#   * NOTHING site-specific is hardcoded — every answer becomes an env var.
#   * Secrets are collected without echo, held in memory, and written ONLY to
#     the gitignored, chmod-600 .env files that docker compose consumes.
#     They are never logged, never committed, never echoed back.
#   * Re-run safe: existing .env files are backed up (chmod 600) first.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

# ---- colors / logging (no secrets ever pass through these) ------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
log()     { echo -e "${CYAN}[$(date '+%H:%M:%S')]${NC} $*"; }
success() { echo -e "${GREEN}[$(date '+%H:%M:%S')] $*${NC}"; }
warn()    { echo -e "${YELLOW}[$(date '+%H:%M:%S')] $*${NC}"; }
error()   { echo -e "${RED}[$(date '+%H:%M:%S')] $*${NC}"; }
header()  { echo -e "\n${BOLD}${CYAN}==============================================================${NC}"
            echo -e "${BOLD}${CYAN}  $*${NC}"
            echo -e "${BOLD}${CYAN}==============================================================${NC}\n"; }
die()     { error "$1"; exit 1; }

# ask VAR "prompt" "default"        — plain answer
ask() {
  local __var="$1" prompt="$2" default="${3:-}" reply
  if [[ -n "$default" ]]; then
    read -rp "$(echo -e "${BOLD}${prompt}${NC} [${default}]: ")" reply
    printf -v "$__var" '%s' "${reply:-$default}"
  else
    read -rp "$(echo -e "${BOLD}${prompt}${NC}: ")" reply
    printf -v "$__var" '%s' "$reply"
  fi
}

# ask_secret VAR "prompt"           — no echo, confirmed, may be left empty
ask_secret() {
  local __var="$1" prompt="$2" s1 s2
  while true; do
    read -rsp "$(echo -e "${BOLD}${prompt}${NC} (input hidden): ")" s1; echo ""
    [[ -z "$s1" ]] && { printf -v "$__var" '%s' ""; return 0; }
    read -rsp "$(echo -e "${BOLD}  confirm${NC}: ")" s2; echo ""
    [[ "$s1" == "$s2" ]] && { printf -v "$__var" '%s' "$s1"; return 0; }
    warn "entries do not match — try again"
  done
}

ask_yes_no() {
  local prompt="$1" default="${2:-N}" reply
  read -rp "$(echo -e "${BOLD}${prompt}${NC} [y/N]: ")" reply
  reply="${reply:-$default}"
  [[ "${reply,,}" =~ ^(y|yes)$ ]]
}

# backup_env <file>  — keep the previous rendered env, still chmod 600
backup_env() {
  local f="$1"
  if [[ -f "$f" ]]; then
    local b="${f}.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p "$f" "$b" && chmod 600 "$b"
    warn "existing $(basename "$f") backed up to $(basename "$b")"
  fi
}

# =============================================================================
# 0. Prerequisites
# =============================================================================
header "NVIS Observability Appliance — installer 1.0.0"
echo -e "Templates in this repo stay untouched; your answers land only in"
echo -e "gitignored ${BOLD}.env${NC} files (chmod 600). Ctrl-C any time — nothing is"
echo -e "deployed until the interview finishes.\n"

log "checking prerequisites (installer/check-prereqs.sh)"
bash "${SCRIPT_DIR}/check-prereqs.sh" || die "prerequisites not met — fix the items above and re-run"

# =============================================================================
# 1. Interview — core platform
# =============================================================================
header "1/3 Interview — core platform"

while true; do
  ask CLUSTER_NAME "Cluster name (lowercase, alnum + dashes; the cluster= label on everything)" "dgx-superpod"
  [[ "$CLUSTER_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] && break
  warn "invalid — use lowercase letters, digits and dashes only"
done

ask DGX_FLAVOR "DGX hardware flavor (B300|B200|B100|H200|H100|A100|custom)" "B300"

while true; do
  ask_secret GRAFANA_ADMIN_PASSWORD "Grafana admin password"
  [[ -z "$GRAFANA_ADMIN_PASSWORD" ]] && { warn "a Grafana admin password is required"; continue; }
  [[ "$GRAFANA_ADMIN_PASSWORD" == "changeme" ]] && { warn "'changeme' is not a password"; continue; }
  [[ "${#GRAFANA_ADMIN_PASSWORD}" -lt 8 ]] && { warn "use at least 8 characters"; continue; }
  break
done

ask DATA_RETENTION_MONTHS "Long-term metrics retention (months, VictoriaMetrics)" "12"
ask PROM_SCRAPE_INTERVAL "Prometheus global scrape interval" "15s"
ask EXPORTER_HOST "Exporter host as seen from the Prometheus container (keep default for a single-host deploy)" "host.docker.internal"
VM_REMOTE_WRITE_URL="http://victoriametrics:8428/api/v1/write"

# =============================================================================
# 1b. Interview — optional integrations
# =============================================================================
header "2/3 Interview — optional integrations"
echo -e "${DIM}Answer 'n' to anything you don't run — you can re-run this installer later.${NC}\n"

# ---- BMC / Redfish -----------------------------------------------------------
HAS_BMC=false
BMC_USER="root"; BMC_PASSWORD=""; BMC_TARGETS_SRC=""; BMC_HOSTS_INLINE=""
if ask_yes_no "Monitor node BMCs over Redfish (temps/power/fans/SEL, exporter :9104)?"; then
  HAS_BMC=true
  ask BMC_USER "BMC username" "root"
  while true; do
    ask_secret BMC_PASSWORD "BMC password"
    [[ -n "$BMC_PASSWORD" ]] && break
    warn "the Redfish exporter needs the BMC password"
  done
  echo -e "${DIM}Target list format: see exporters/redfish-bmc/bmc-targets.example.csv"
  echo -e "(node,bmc_ip per line). It is written to ./secrets/ — gitignored.${NC}"
  ask BMC_TARGETS_SRC "Path to your bmc-targets CSV (empty to type hosts inline)" ""
  if [[ -z "$BMC_TARGETS_SRC" ]]; then
    ask BMC_HOSTS_INLINE "Comma-separated BMC IPs/hosts (e.g. 198.51.100.1,198.51.100.2)" ""
    [[ -z "$BMC_HOSTS_INLINE" ]] && { warn "no BMC targets given — disabling the Redfish exporter"; HAS_BMC=false; }
  elif [[ ! -f "$BMC_TARGETS_SRC" ]]; then
    die "BMC target CSV not found: $BMC_TARGETS_SRC"
  fi
fi

# ---- BCM ----------------------------------------------------------------------
HAS_BCM=false
BCM_HOST=""; BCM_USER="root"; BCM_JUMP=""
if ask_yes_no "Monitor Base Command Manager — BCM node inventory (SSH-key auth, exporter :9401)?"; then
  HAS_BCM=true
  while [[ -z "$BCM_HOST" ]]; do ask BCM_HOST "BCM head node host/IP" ""; done
  ask BCM_USER "BCM SSH user (key-based auth; the exporter mounts ~/.ssh read-only)" "root"
  ask BCM_JUMP "Optional SSH jump host (user@host, empty for direct)" ""
fi

# ---- UFM / InfiniBand fabric ---------------------------------------------------
HAS_UFM=false
UFM_HOST=""; UFM_USER="ufmsystem"; UFM_PASSWORD=""
SWITCH_SSH_USER=""; SWITCH_SSH_PASSWORD=""; FABRIC="fabric"; HOST_NAME_PREFIX="gpu"
UFM_TELEMETRY_PORT="9001"
if ask_yes_no "Monitor the InfiniBand fabric via NVIDIA UFM (6 exporters: UFM/telemetry/LLDP/cable/identity/switch-audit)?"; then
  HAS_UFM=true
  while [[ -z "$UFM_HOST" ]]; do ask UFM_HOST "UFM server host/IP" ""; done
  ask UFM_USER "UFM REST username" "ufmsystem"
  while true; do
    ask_secret UFM_PASSWORD "UFM REST password"
    [[ -n "$UFM_PASSWORD" ]] && break
    warn "the UFM exporters need the REST password"
  done
  ask UFM_TELEMETRY_PORT "UFM Telemetry CSV port (open endpoint on the UFM host)" "9001"
  ask FABRIC "fabric= label for this IB fabric (e.g. compute / east-west)" "fabric"
  ask HOST_NAME_PREFIX "Compute-host hostname prefix (for ib-identity host matching)" "gpu"
  if ask_yes_no "Enable the switch SSH sweep (per-switch transceiver/LLDP detail; needs NVOS SSH creds)?"; then
    ask SWITCH_SSH_USER "Switch SSH username" "admin"
    ask_secret SWITCH_SSH_PASSWORD "Switch SSH password"
    [[ -z "$SWITCH_SSH_PASSWORD" ]] && { warn "no switch password — SSH sweep stays disabled"; SWITCH_SSH_USER=""; }
  fi
fi

# ---- NetQ ----------------------------------------------------------------------
HAS_NETQ=false
NETQ_ENDPOINT=""; NETQ_TOKEN=""; NETQ_USER=""; NETQ_PASS=""
if ask_yes_no "Monitor an Ethernet/Cumulus fabric via NVIDIA NetQ (exporter :9109)?"; then
  HAS_NETQ=true
  while [[ -z "$NETQ_ENDPOINT" ]]; do ask NETQ_ENDPOINT "NetQ API base URL (https://...)" ""; done
  ask_secret NETQ_TOKEN "NetQ bearer token (preferred; empty to use user/password)"
  if [[ -z "$NETQ_TOKEN" ]]; then
    ask NETQ_USER "NetQ username" ""
    ask_secret NETQ_PASS "NetQ password"
    [[ -z "$NETQ_USER" || -z "$NETQ_PASS" ]] && { warn "no NetQ credentials — disabling NetQ"; HAS_NETQ=false; }
  fi
fi

# ---- Slurm job monitor -----------------------------------------------------------
HAS_JOBMON=false
SLURM_JOB_LOG_DIR=""; SLURM_LOG_GLOB='workloads/*/experiments/*/*/*/log-*_*_0.out'
SLURM_CONF=""; SLURM_MODE=""
if ask_yes_no "Enable the Slurm job monitor (llmb-job :9119 + slurm-job :9402 — per-job progress/stall/NaN)?"; then
  HAS_JOBMON=true
  while true; do
    ask SLURM_JOB_LOG_DIR "Absolute path to the shared-FS root holding per-job benchmark logs" ""
    [[ "$SLURM_JOB_LOG_DIR" == /* ]] && break
    warn "must be an absolute path (it is bind-mounted into the container)"
  done
  [[ -d "$SLURM_JOB_LOG_DIR" ]] || warn "$SLURM_JOB_LOG_DIR does not exist on this host yet — the exporter will wait for it"
  ask SLURM_LOG_GLOB "Training-stdout log glob (relative to that root)" "$SLURM_LOG_GLOB"
  ask SLURM_CONF "Absolute path to slurm.conf (empty if squeue/sacct resolve via DNS SRV)" ""
  if [[ "$HAS_BCM" == true ]]; then
    SLURM_MODE=""   # auto: ssh via BCM_HOST
    log "slurm-job-exporter will reach squeue/sacct over SSH via BCM_HOST=${BCM_HOST}"
  else
    ask SLURM_MODE "slurm-job-exporter mode: 'local' (this host runs squeue) or 'ssh'" "local"
    if [[ "$SLURM_MODE" == "ssh" ]]; then
      while [[ -z "$BCM_HOST" ]]; do ask BCM_HOST "Slurm controller SSH host/IP" ""; done
    fi
  fi
fi

# ---- NVLMapper --------------------------------------------------------------------
HAS_NVLMAPPER=false
NVLMAPPER_BMC_USER=""; NVLMAPPER_BMC_PASSWORD=""; NVLINK_FLAVOR="nvl72"
if ask_yes_no "Enable NVLMapper (switch-side NVLink down-lane mapper for NVL rack trays, exporter :9120)?"; then
  HAS_NVLMAPPER=true
  ask NVLINK_FLAVOR "NVLink fabric generation label (flavor=)" "nvl72"
  ask NVLMAPPER_BMC_USER "Default NVSwitch tray BMC username" ""
  ask_secret NVLMAPPER_BMC_PASSWORD "Default NVSwitch tray BMC password"
  if [[ ! -f nvlmapper/config.yml ]]; then
    cp nvlmapper/config.yml.template nvlmapper/config.yml
    chmod 600 nvlmapper/config.yml
    warn "wrote nvlmapper/config.yml from the template — EDIT IT with your rack/tray BMC list before the sweep can run (gitignored)"
  fi
fi

# ---- Node agents ---------------------------------------------------------------------
HAS_NODE_AGENTS=false
METRICS_TARGET=""; RELAY_HOST=""
if ask_yes_no "Configure the per-node agents env (DCGM/node_exporter/cgroup/Alloy rollout — see node-agents/)?"; then
  HAS_NODE_AGENTS=true
  default_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  ask METRICS_TARGET "This observability host's IP/hostname as reachable from the head node" "${default_ip:-}"
  ask RELAY_HOST "Head-node relay host/IP (set = METRICS_TARGET if nodes reach this host directly)" "${METRICS_TARGET}"
fi

# =============================================================================
# 2. Write the gitignored env files
# =============================================================================
header "3/3 Render config + deploy"

umask 077   # every file created below is 0600/0700

# ---- platform/.env -----------------------------------------------------------
backup_env platform/.env
cat > platform/.env <<EOF
# Rendered by installer/install.sh $(date -u +%Y-%m-%dT%H:%M:%SZ) — GITIGNORED, chmod 600.
# Template: platform/.env.example
CLUSTER_NAME=${CLUSTER_NAME}
DGX_FLAVOR=${DGX_FLAVOR}
GRAFANA_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}
DATA_RETENTION_MONTHS=${DATA_RETENTION_MONTHS}
EXPORTER_HOST=${EXPORTER_HOST}
PROM_SCRAPE_INTERVAL=${PROM_SCRAPE_INTERVAL}
VM_REMOTE_WRITE_URL=${VM_REMOTE_WRITE_URL}
EOF
success "wrote platform/.env"

# ---- render prometheus.yml from the template ----------------------------------
export CLUSTER_NAME PROM_SCRAPE_INTERVAL EXPORTER_HOST VM_REMOTE_WRITE_URL
if command -v envsubst >/dev/null 2>&1; then
  envsubst '${CLUSTER_NAME} ${PROM_SCRAPE_INTERVAL} ${EXPORTER_HOST} ${VM_REMOTE_WRITE_URL}' \
    < platform/prometheus/prometheus.yml.tmpl > platform/prometheus/prometheus.yml
else
  sed -e "s|\${CLUSTER_NAME}|${CLUSTER_NAME}|g" \
      -e "s|\${PROM_SCRAPE_INTERVAL}|${PROM_SCRAPE_INTERVAL}|g" \
      -e "s|\${EXPORTER_HOST}|${EXPORTER_HOST}|g" \
      -e "s|\${VM_REMOTE_WRITE_URL}|${VM_REMOTE_WRITE_URL}|g" \
      platform/prometheus/prometheus.yml.tmpl > platform/prometheus/prometheus.yml
fi
chmod 644 platform/prometheus/prometheus.yml   # no secrets in it; container reads it
success "rendered platform/prometheus/prometheus.yml"

# ---- root .env (fabric + exporter + nvlmapper + node-agent fragments) ----------
backup_env .env
{
  echo "# Rendered by installer/install.sh $(date -u +%Y-%m-%dT%H:%M:%SZ) — GITIGNORED, chmod 600."
  echo "# Templates: .env.fabric.example, exporters/.env.*.example, node-agents/.env.node-agents.example"
  echo "CLUSTER_NAME=${CLUSTER_NAME}"
  echo "DGX_FLAVOR=${DGX_FLAVOR}"

  if [[ "$HAS_BMC" == true ]]; then
    echo ""
    echo "# ---- Redfish BMC exporter (:9104) ----"
    echo "BMC_USER=${BMC_USER}"
    echo "BMC_PASSWORD=${BMC_PASSWORD}"
    echo "BMC_CONCURRENCY=16"
    echo "BMC_SENSOR_CONCURRENCY=8"
    echo "BMC_VERIFY_SSL=false"
    echo "BMC_SCRAPE_INTERVAL=300"
    echo "BMC_TIMEOUT=12"
    echo "BMC_FW_DISABLED=true"
    echo "BMC_SEL_DISABLED=false"
  fi

  if [[ "$HAS_BCM" == true || -n "$BCM_HOST" ]]; then
    echo ""
    echo "# ---- BCM exporter (:9401) / Slurm SSH target ----"
    echo "BCM_HOST=${BCM_HOST}"
    echo "BCM_USER=${BCM_USER}"
    echo "BCM_JUMP=${BCM_JUMP}"
    echo "BCM_SCRAPE_INTERVAL=60"
  fi

  if [[ "$HAS_UFM" == true ]]; then
    echo ""
    echo "# ---- UFM / IB fabric exporters (see .env.fabric.example) ----"
    echo "UFM_HOST=${UFM_HOST}"
    echo "UFM_USER=${UFM_USER}"
    echo "UFM_PASSWORD=${UFM_PASSWORD}"
    echo "UFM_VERIFY_SSL=false"
    echo "UFM_VERIFY_SSL_NUM=0"
    echo "UFM_CLUSTER_MINDER=false"
    echo "UFM_TELEMETRY_PORT=${UFM_TELEMETRY_PORT}"
    echo "UFM_TELEMETRY_URL="
    echo "UFM_TELEMETRY_HOST_FILTER="
    echo "SWITCH_SSH_USER=${SWITCH_SSH_USER}"
    echo "SWITCH_SSH_PASSWORD=${SWITCH_SSH_PASSWORD}"
    echo "SWITCH_LIST="
    echo "FABRIC=${FABRIC}"
    echo "HOST_NAME_PREFIX=${HOST_NAME_PREFIX}"
    echo "UFM_SCRAPE_INTERVAL=30"
    echo "UFM_TELEMETRY_POLL=90"
    echo "IB_LLDP_SWEEP_ENABLED=$([[ -n "$SWITCH_SSH_USER" ]] && echo 1 || echo 0)"
    echo "IB_LLDP_SWEEP_INTERVAL=1800"
    echo "CABLE_SCAN_MODE=ufm"
    echo "CABLE_AUDIT_INTERVAL=43200"
    echo "CABLE_ISSUE_REFRESH=60"
    echo "CABLE_ISSUE_LOKI_URL="
    echo "SWITCH_AUDIT_REFRESH=60"
    echo "IB_IDENTITY_REFRESH=3600"
  fi

  if [[ "$HAS_NETQ" == true ]]; then
    echo ""
    echo "# ---- NetQ exporter (:9109) ----"
    echo "NETQ_ENDPOINT=${NETQ_ENDPOINT}"
    echo "NETQ_TOKEN=${NETQ_TOKEN}"
    echo "NETQ_USER=${NETQ_USER}"
    echo "NETQ_PASS=${NETQ_PASS}"
    echo "NETQ_VERIFY_SSL=true"
    echo "NETQ_SCRAPE_INTERVAL=30"
  fi

  if [[ "$HAS_JOBMON" == true ]]; then
    echo ""
    echo "# ---- Job monitor (llmb-job :9119, slurm-job :9402) ----"
    echo "SLURM_JOB_LOG_DIR=${SLURM_JOB_LOG_DIR}"
    echo "SLURM_LOG_GLOB=${SLURM_LOG_GLOB}"
    echo "SLURM_REGISTRY_SQLITE="
    echo "SLURM_MODE=${SLURM_MODE}"
    echo "SLURM_BIN="
    echo "SLURM_CONF=${SLURM_CONF}"
    echo "BCM_SSH_PORT=22"
    echo "SACCT_HOURS=24"
    echo "SLURM_SCRAPE_INTERVAL=30"
    echo "LLMB_EXPORTER_INTERVAL=30"
  fi

  if [[ "$HAS_NVLMAPPER" == true ]]; then
    echo ""
    echo "# ---- NVLMapper (:9120) — rack/tray list lives in nvlmapper/config.yml ----"
    echo "NVLMAPPER_ENABLED=true"
    echo "NVLINK_FLAVOR=${NVLINK_FLAVOR}"
    echo "NVLMAPPER_BMC_USER=${NVLMAPPER_BMC_USER}"
    echo "NVLMAPPER_BMC_PASSWORD=${NVLMAPPER_BMC_PASSWORD}"
    echo "NVLMAPPER_PORT=9120"
    echo "NVLMAPPER_RUN_BINARY=false"
    echo "NVLMAPPER_SWEEP_SCOPE=rack"
  fi

  if [[ "$HAS_NODE_AGENTS" == true ]]; then
    echo ""
    echo "# ---- node-agents rollout (see node-agents/.env.node-agents.example) ----"
    echo "METRICS_TARGET=${METRICS_TARGET}"
    echo "RELAY_HOST=${RELAY_HOST}"
    echo "RELAY_RW_PORT=19090"
    echo "RELAY_LOKI_PORT=13100"
    echo "NODE_LIST=./node-agents/nodes.txt"
    echo "SSH_USER=root"
    echo "SSH_JUMP="
    echo "SHARE=/cm/shared/nvis/bin"
    echo "TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector"
    echo "COLLECTOR_INTERVAL=30s"
    echo "ENABLE_TEXTFILE_COLLECTORS=1"
  fi
} > .env
success "wrote .env (repo root)"

# ---- BMC target CSV (gitignored ./secrets/) -----------------------------------
if [[ "$HAS_BMC" == true ]]; then
  mkdir -p secrets && chmod 700 secrets
  if [[ -n "$BMC_TARGETS_SRC" ]]; then
    cp "$BMC_TARGETS_SRC" secrets/bmc-targets.csv
  else
    {
      echo "node,bmc_ip"
      i=1
      IFS=',' read -ra hosts <<< "$BMC_HOSTS_INLINE"
      for h in "${hosts[@]}"; do
        h="$(echo "$h" | xargs)"; [[ -z "$h" ]] && continue
        printf 'node%03d,%s\n' "$i" "$h"; i=$((i+1))
      done
    } > secrets/bmc-targets.csv
  fi
  chmod 600 secrets/bmc-targets.csv
  success "wrote secrets/bmc-targets.csv ($(($(wc -l < secrets/bmc-targets.csv) - 1)) targets, gitignored)"
fi

# =============================================================================
# 3. Deploy
# =============================================================================
COMPOSE_FILES=()
[[ "$HAS_UFM" == true ]]       && COMPOSE_FILES+=(-f docker-compose.fabric.yml)
{ [[ "$HAS_BMC" == true ]] || [[ "$HAS_BCM" == true ]] || [[ "$HAS_NETQ" == true ]]; } \
                               && COMPOSE_FILES+=(-f exporters/compose.bmc-bcm-netq.yml)
[[ "$HAS_JOBMON" == true ]]    && COMPOSE_FILES+=(-f exporters/compose.job-monitor.yaml)
[[ "$HAS_NVLMAPPER" == true ]] && COMPOSE_FILES+=(-f nvlmapper/docker-compose.nvlmapper.yml)

echo ""
log "deploy plan:"
echo "  * central stack        platform/docker-compose.yml (always)"
for f in "${COMPOSE_FILES[@]}"; do [[ "$f" == -f ]] && continue; echo "  * exporter fragment    $f"; done
[[ "$HAS_NODE_AGENTS" == true ]] && echo "  * node agents          manual rollout (instructions below)"
echo ""

if ! ask_yes_no "Deploy now with docker compose?" "y"; then
  warn "skipping deploy — configs are written; re-run this installer or start manually:"
  echo "    (cd platform && docker compose --env-file .env up -d)"
  [[ ${#COMPOSE_FILES[@]} -gt 0 ]] && echo "    docker compose --project-directory . ${COMPOSE_FILES[*]} up -d"
  exit 0
fi

log "starting the central stack (VictoriaMetrics + Prometheus + Loki + Grafana + Alloy)"
(cd platform && docker compose --env-file .env up -d) || die "central stack failed to start — see 'docker compose logs' in platform/"
success "central stack up"

if [[ ${#COMPOSE_FILES[@]} -gt 0 ]]; then
  log "starting exporter fragments"
  docker compose --project-directory . "${COMPOSE_FILES[@]}" up -d \
    || die "exporter fragment failed — check 'docker compose --project-directory . ${COMPOSE_FILES[*]} logs'"
  success "exporters up"
fi

# ---- smoke check ----------------------------------------------------------------
log "waiting for Grafana to answer"
for _ in $(seq 1 30); do
  curl -sf -o /dev/null http://localhost:3000/api/health && break
  sleep 2
done
curl -sf -o /dev/null http://localhost:3000/api/health \
  && success "Grafana healthy on :3000" \
  || warn "Grafana not answering yet on :3000 — check 'docker logs nvis-grafana'"
curl -sf -o /dev/null http://localhost:9090/-/ready   && success "Prometheus ready on :9090"       || warn "Prometheus not ready on :9090"
curl -sf -o /dev/null http://localhost:8428/health    && success "VictoriaMetrics healthy on :8428" || warn "VictoriaMetrics not answering on :8428"
curl -sf -o /dev/null http://localhost:3100/ready     && success "Loki ready on :3100"              || warn "Loki not ready on :3100 (it can take ~15 s)"

# =============================================================================
# 4. Summary / next steps
# =============================================================================
header "Done"
echo -e "  Grafana     http://<this-host>:3000   (admin / the password you set)"
echo -e "  Prometheus  http://<this-host>:9090"
echo -e "  Dashboards  auto-provisioned into the ${BOLD}NVIS${NC} folder"
echo ""
echo -e "${BOLD}Next steps${NC}"
if [[ "$HAS_NODE_AGENTS" == true ]]; then
  echo "  * Roll the node agents out to the fleet (from node-agents/, see its README):"
  echo "      set -a; . ../.env; set +a"
  echo "      ./head-node/setup-relay.sh --docker     # on the head node (skip if nodes reach this host)"
  echo "      ./stage-binaries.sh                     # host with the cluster share mounted"
  echo "      cp nodes.txt.example nodes.txt          # then list your nodes (or NODE_DISCOVERY_CMD)"
  echo "      ./rollout.sh                            # canary first, then batches"
fi
if [[ "$HAS_NVLMAPPER" == true ]]; then
  echo "  * Edit nvlmapper/config.yml with your rack / NVSwitch-tray BMC inventory, then:"
  echo "      docker compose --project-directory . -f nvlmapper/docker-compose.nvlmapper.yml restart"
fi
if [[ "$HAS_UFM" == true && "$HAS_BCM" != true ]]; then
  echo "  * Exporters you did not enable stay as failing scrape targets in"
  echo "    platform/prometheus/prometheus.yml — comment their jobs out if the 'down' targets bother you."
fi
echo "  * Secrets live ONLY in .env / platform/.env / secrets/ (all gitignored, chmod 600)."
echo "  * Re-run ./installer/install.sh any time to change answers (existing .env files are backed up)."
