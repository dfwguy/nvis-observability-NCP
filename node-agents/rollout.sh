#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# rollout.sh — Onboard NVIS observability agents across the GPU nodes.
# Runs from the ObsVM (or any admin host). Reaches each node over SSH, optionally
# through a head-node jump host, and runs install-node.sh from the shared dir.
#
#   dcgm-exporter :9400 + node_exporter :9100 + cgroup_exporter :9306 + Alloy :12345
#   → remote_write / loki push to RELAY_HOST → ObsVM (METRICS_TARGET)
#
# Prereqs (operator does once): stage binaries + install-node.sh onto the
# cluster-shared dir ($SHARE) visible to every node — see stage-binaries.sh.
#
# ── Node inventory (pick one) ────────────────────────────────────────────────
#   NODE_LIST           path to a file with one node hostname per line
#                       (blank lines and #comments ignored). Default ./nodes.txt
#   NODE_DISCOVERY_CMD  command whose stdout is the node list, one per line, e.g.
#                         NODE_DISCOVERY_CMD='sinfo -h -N -o %N'
#                         NODE_DISCOVERY_CMD='scontrol show hostnames $(sinfo -h -o %N)'
#   NODES               space/newline separated hostnames passed inline
#
# ── SSH reachability ─────────────────────────────────────────────────────────
#   SSH_USER   login user on the nodes                       (default root)
#   SSH_JUMP   ProxyJump spec if nodes are only reachable via a bastion/head,
#              e.g. SSH_JUMP='admin@head.example'            (default: none/direct)
#
# ── Install-node passthrough (exported to each node) ─────────────────────────
#   METRICS_TARGET RELAY_HOST RELAY_RW_PORT RELAY_LOKI_PORT CLUSTER_NAME
#   DGX_FLAVOR SHARE TEXTFILE_DIR FORCE
#
# ── Run controls ─────────────────────────────────────────────────────────────
#   BATCH    parallel nodes per wave                         (default 12)
#   CANARY   onboard this node first and gate on PASS        (default: first in list)
#   RETRIES  ssh attempts per node on transient failures     (default 3)
#   Usage:  bash rollout.sh              # canary, then fleet in batches
#           bash rollout.sh --no-canary  # skip canary gate
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SSH_USER="${SSH_USER:-root}"
SSH_JUMP="${SSH_JUMP:-}"
SHARE="${SHARE:-/cm/shared/nvis/bin}"
BATCH="${BATCH:-12}"
RETRIES="${RETRIES:-3}"
CANARY="${CANARY:-}"
DO_CANARY=1; [[ "${1:-}" == "--no-canary" ]] && DO_CANARY=0

# values forwarded to install-node.sh on each node (only non-empty ones)
PASSTHROUGH=(METRICS_TARGET RELAY_HOST RELAY_RW_PORT RELAY_LOKI_PORT \
             CLUSTER_NAME DGX_FLAVOR SHARE TEXTFILE_DIR FORCE)

RESULTS="$(cd "$(dirname "$0")" && pwd)/rollout-results.tsv"

# ── build the node list ──────────────────────────────────────────────────────
declare -a NODES_ARR=()
if [[ -n "${NODES:-}" ]]; then
  read -r -a NODES_ARR <<<"${NODES}"
elif [[ -n "${NODE_DISCOVERY_CMD:-}" ]]; then
  mapfile -t NODES_ARR < <(bash -c "${NODE_DISCOVERY_CMD}" | sed 's/#.*//' | awk 'NF')
else
  NODE_LIST="${NODE_LIST:-$(cd "$(dirname "$0")" && pwd)/nodes.txt}"
  [[ -f "${NODE_LIST}" ]] || { echo "ERROR: no inventory — set NODES, NODE_DISCOVERY_CMD, or provide ${NODE_LIST}" >&2; exit 1; }
  mapfile -t NODES_ARR < <(sed 's/#.*//' "${NODE_LIST}" | awk 'NF')
fi
[[ "${#NODES_ARR[@]}" -gt 0 ]] || { echo "ERROR: node inventory is empty" >&2; exit 1; }
[[ -z "${CANARY}" ]] && CANARY="${NODES_ARR[0]}"

# ── remote install command (env passthrough, then run installer from SHARE) ──
build_env_prefix(){
  local kv="" v
  for k in "${PASSTHROUGH[@]}"; do
    v="${!k:-}"; [[ -z "${v}" ]] && continue
    kv+="${k}=$(printf '%q' "${v}") "
  done
  printf '%s' "${kv}"
}
ENV_PREFIX="$(build_env_prefix)"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=20 -o ServerAliveInterval=10 -o BatchMode=yes)
[[ -n "${SSH_JUMP}" ]] && SSH_OPTS+=(-J "${SSH_JUMP}")

run_node(){ # <hostname> -> prints "<host>\t<verdict>\t<detail>"
  local host="$1" out line verdict detail attempt
  for attempt in $(seq 1 "${RETRIES}"); do
    sleep "0.$(( RANDOM % 9 ))"   # jitter to spread load on the jump host sshd
    out="$(ssh "${SSH_OPTS[@]}" "${SSH_USER}@${host}" \
            "${ENV_PREFIX} bash ${SHARE}/install-node.sh 2>/dev/null" 2>&1)"
    line="$(printf '%s\n' "$out" | grep -m1 '^NVIS_RESULT')"
    [[ -n "$line" ]] && break
    if printf '%s' "$out" | grep -qE 'Connection reset|kex_exchange|Connection closed|timed out|Connection refused'; then
      sleep $(( attempt * 3 )); continue
    fi
    break
  done
  if [[ -n "$line" ]]; then
    verdict="$(sed -n 's/.*verdict=\([A-Z]*\).*/\1/p' <<<"$line")"
    detail="$(sed -n 's/^NVIS_RESULT host=[^ ]* //p' <<<"$line")"
  else
    verdict="FAIL"; detail="no_result_line: $(printf '%s' "$out" | tr '\n' '|' | tail -c 160)"
  fi
  printf '%s\t%s\t%s\n' "$host" "${verdict:-FAIL}" "$detail"
}

echo "════════════════════════════════════════════════════════════════"
echo "  NVIS node rollout"
echo "  nodes=${#NODES_ARR[@]}  batch=${BATCH}  canary=$([[ ${DO_CANARY} == 1 ]] && echo "${CANARY}" || echo off)"
echo "  jump=${SSH_JUMP:-<direct>}  share=${SHARE}"
echo "════════════════════════════════════════════════════════════════"
: > "${RESULTS}"

# ── canary gate ──────────────────────────────────────────────────────────────
if [[ "${DO_CANARY}" == 1 ]]; then
  echo ">> CANARY: ${CANARY}"
  res="$(run_node "${CANARY}")"; echo "   ${res}"; echo "${res}" >> "${RESULTS}"
  if [[ "$(cut -f2 <<<"$res")" != "PASS" ]]; then
    echo "!! CANARY FAILED — aborting rollout. Inspect ${CANARY} before continuing." >&2
    exit 1
  fi
  echo ">> canary PASS — proceeding to fleet"
fi

# ── fleet in bounded-parallel batches ────────────────────────────────────────
declare -a NODES=()
for n in "${NODES_ARR[@]}"; do
  [[ "${DO_CANARY}" == 1 && "$n" == "${CANARY}" ]] && continue   # already done
  NODES+=("$n")
done

pass=0; fail=0
tmpd="$(mktemp -d)"
total=${#NODES[@]}; idx=0
while (( idx < total )); do
  batch=( "${NODES[@]:idx:BATCH}" )
  echo ">> batch $((idx/BATCH+1)): ${batch[*]}"
  pids=()
  for n in "${batch[@]}"; do
    ( run_node "$n" > "${tmpd}/${n}" ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p"; done
  for n in "${batch[@]}"; do
    r="$(cat "${tmpd}/${n}" 2>/dev/null)"
    [[ -z "$r" ]] && r="$(printf '%s\tFAIL\tno_output' "$n")"
    echo "${r}" >> "${RESULTS}"
    v="$(cut -f2 <<<"$r")"
    if [[ "$v" == "PASS" ]]; then ((pass++)); printf '   [PASS] %s\n' "$n"
    else ((fail++)); printf '   [FAIL] %s :: %s\n' "$n" "$(cut -f3 <<<"$r")"; fi
  done
  idx=$((idx+BATCH))
done
rm -rf "${tmpd}"

echo "════════════════════════════════════════════════════════════════"
echo "  RESULT: PASS=${pass}  FAIL=${fail}  (canary counted separately)"
echo "  matrix: ${RESULTS}"
echo "════════════════════════════════════════════════════════════════"
[[ "${fail}" -eq 0 ]] && exit 0 || exit 1
