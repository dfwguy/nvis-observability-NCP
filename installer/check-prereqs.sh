#!/usr/bin/env bash
# =============================================================================
# NVIS Observability Appliance — prerequisite checker
#
# Verifies the observability host can run the appliance BEFORE the interview
# installer makes any changes. Safe to run standalone:
#
#   ./installer/check-prereqs.sh          # exit 0 = ready, exit 1 = fix & re-run
#
# Checks: OS family, docker engine, docker compose v2, curl, python3,
# free disk, and that the core stack ports are not already taken.
# Read-only — this script changes NOTHING on the host.
# =============================================================================
set -uo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}[ok]${NC}   $*"; }
bad()  { echo -e "  ${RED}[FAIL]${NC} $*"; FAILURES=$((FAILURES + 1)); }
note() { echo -e "  ${YELLOW}[warn]${NC} $*"; }

FAILURES=0

echo -e "${BOLD}NVIS prerequisite check${NC}"

# ---- OS family (informational — the stack is containerised) -----------------
if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian|centos|rhel|rocky|almalinux) ok "OS: ${PRETTY_NAME:-$ID} (supported)" ;;
    *) note "OS: ${PRETTY_NAME:-unknown} — untested, containers should still run" ;;
  esac
else
  note "no /etc/os-release — unknown OS, continuing"
fi

# ---- required commands -------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "docker engine running ($(docker --version | cut -d, -f1))"
  else
    bad "docker installed but the daemon is unreachable (start dockerd / add user to the docker group)"
  fi
else
  bad "docker not found — install Docker Engine: https://docs.docker.com/engine/install/"
fi

if docker compose version >/dev/null 2>&1; then
  ok "docker compose v2 ($(docker compose version --short 2>/dev/null || echo present))"
else
  bad "docker compose v2 plugin not found — https://docs.docker.com/compose/install/"
fi

for cmd in curl python3; do
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd present"
  else
    bad "$cmd not found — install it with your package manager"
  fi
done

command -v envsubst >/dev/null 2>&1 \
  && ok "envsubst present (gettext)" \
  || note "envsubst not found — installer falls back to a built-in renderer"

# ---- disk space (metrics/log retention lives under docker volumes) ----------
avail_gb=$(df -BG --output=avail /var/lib/docker 2>/dev/null | tail -1 | tr -dc '0-9')
[[ -z "$avail_gb" ]] && avail_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if [[ "${avail_gb:-0}" -ge 100 ]]; then
  ok "disk: ${avail_gb}G free for docker volumes"
elif [[ "${avail_gb:-0}" -ge 30 ]]; then
  note "disk: only ${avail_gb}G free — fine for a trial, plan >=100G for real retention"
else
  bad "disk: ${avail_gb:-0}G free — the TSDB/log stores need at least 30G"
fi

# ---- core ports free ---------------------------------------------------------
#   3000 grafana · 9090 prometheus · 8428 victoriametrics · 3100 loki
#   12345/9099/9098 alloy gateway
for port in 3000 9090 8428 3100 12345 9099 9098; do
  if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
    exec 3>&- 3<&-
    bad "port ${port} already in use — the central stack needs it"
  else
    ok "port ${port} free"
  fi
done

echo ""
if [[ "$FAILURES" -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}All prerequisites satisfied.${NC}"
  exit 0
else
  echo -e "${RED}${BOLD}${FAILURES} prerequisite(s) failed — fix the items above and re-run.${NC}"
  exit 1
fi
