#!/usr/bin/env bash
# =============================================================================
# hca_verbs_collector.sh — per-NODE IB HCA verbs/port health, emitted in
# Prometheus node_exporter TEXTFILE format so the platform scrapes it with ZERO
# new exporter (labeled by the node the exporter runs on).
#
# This is the "slow-but-no-switch-error" IB signal a microbenchmark misses: RC
# retransmission / ACK-timeout / CQE-error hw_counters + port_xmit_wait live on
# the HCA, not on the switch. Complements host_nvlink_collector.sh (GPU-side
# NVLink) and the central switch/UFM counters.
#
# Reads /sys/class/infiniband/mlx5_*/ and writes ONE .prom atomically (tmp + mv).
# Only IB HCAs are emitted — RoCE bond/Ethernet ports (link_layer=Ethernet) are
# skipped so a storage bond does not mix into the compute-fabric signal.
#
# Idempotent, stateless, safe to run every 30s. Root is NOT required (sysfs
# counters are world-readable), but the textfile dir must be writable.
#
# Deploy: install-node.sh copies this to /usr/local/bin and runs it on a systemd
# timer, writing into the node_exporter textfile dir. To run it by hand across
# the fleet use any parallel-ssh tool, e.g.:
#   pdsh -w "$NODE_LIST" 'TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector \
#         /usr/local/bin/hca_verbs_collector.sh'
# node_exporter must run with --collector.textfile.directory=$TEXTFILE_DIR.
#
# Coverage series (ALWAYS emitted, even when zero IB HCAs match) so a silent node
# is distinguishable from a healthy zero-retransmit node — blank != clean:
#   count(ib_hca_collector_up{...})          per node -> proves the collector RAN
#   min(ib_hca_discovered_total{...}) == 0   -> ALERT: ran but found no IB HCAs
# =============================================================================
set -uo pipefail

# ---- parameters (override via env in the systemd unit or --setenv) -----------
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"
IB_SYS_DIR="${IB_SYS_DIR:-/sys/class/infiniband}"   # parameterized for self-test

OUT="${TEXTFILE_DIR}/ib_hca.prom"
TMP="${OUT}.$$"

mkdir -p "$TEXTFILE_DIR" 2>/dev/null || true
: > "$TMP" || { echo "hca_verbs_collector: cannot write $TMP" >&2; exit 1; }
trap 'rm -f "$TMP"' EXIT

emit(){ printf '%s\n' "$1" >> "$TMP"; }

# Read a single unsigned-integer sysfs value. Returns 1 (skip the line) if the
# file is absent/unreadable or the content is not a plain non-negative integer.
# NOTE: `read` returns exit 1 on a final line with NO trailing newline yet still
# populates $v, so we do NOT gate on read's status — we gate on the value. This
# makes a newline-less sysfs counter still emit instead of silently vanishing.
read_uint(){
  local f="$1" v=""
  [ -r "$f" ] || return 1
  IFS= read -r v < "$f" 2>/dev/null
  case "$v" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s' "$v"
}

# ---- discover the IB HCAs (skip RoCE: link_layer != InfiniBand) --------------
# The mlx5_* glob also matches the RoCE bond (mlx5_bond_0) and split-port RoCE
# devices; the link_layer gate is the authoritative filter. Devices without a
# ports/1 (rare mgmt/aux devices) are skipped too.
IB_HCAS=()
for dev in "${IB_SYS_DIR}"/mlx5_*; do
  [ -e "$dev" ] || continue                 # literal glob when nothing matches
  ll_file="${dev}/ports/1/link_layer"
  [ -r "$ll_file" ] || continue
  # Do NOT `|| continue` on read: a newline-less final line makes read exit 1
  # while still setting $layer='InfiniBand'. Gate on the VALUE; pre-clear so an
  # unreadable file leaves it empty and is skipped.
  layer=""
  IFS= read -r layer < "$ll_file" 2>/dev/null
  [ "$layer" = "InfiniBand" ] || continue   # skip Ethernet / RoCE
  IB_HCAS+=("$(basename "$dev")")
done

# ---- per-node heartbeat + coverage (blank != clean) -------------------------
# ALWAYS emitted, even when the link_layer gate matched ZERO IB HCAs. Without
# this a node that ran but found no IB HCAs writes a well-formed, fresh .prom
# with only HELP/TYPE lines and zero samples — indistinguishable (scrape ok) from
# a healthy node with zero retransmits. These two series give downstream a
# per-node signal: presence proves the collector RAN on this node, and
# ib_hca_discovered_total==0 flags 'ran, found no IB HCAs' vs series simply
# absent (collector never ran / node down).
emit "# HELP ib_hca_collector_up hca_verbs_collector ran to completion on this node (always 1)."
emit "# TYPE ib_hca_collector_up gauge"
emit "ib_hca_collector_up 1"
emit "# HELP ib_hca_discovered_total IB HCAs (link_layer=InfiniBand) discovered and emitted this run."
emit "# TYPE ib_hca_discovered_total gauge"
emit "ib_hca_discovered_total ${#IB_HCAS[@]}"

# Emit HELP/TYPE for a metric, then one guarded sample per IB HCA.
#   $1 = ports/1 subdir (counters | hw_counters)   $2 = counter file/name
#   $3 = HELP text
emit_counter(){
  local subdir="$1" name="$2" help="$3" hca val
  emit "# HELP ib_hca_${name} ${help}"
  emit "# TYPE ib_hca_${name} counter"
  if (( ${#IB_HCAS[@]} )); then
    for hca in "${IB_HCAS[@]}"; do
      val="$(read_uint "${IB_SYS_DIR}/${hca}/ports/1/${subdir}/${name}")" || continue
      emit "ib_hca_${name}{hca=\"${hca}\"} ${val}"
    done
  fi
}

# ---- standard IB port counters (monotonic) ----------------------------------
emit_counter counters port_xmit_data \
  "PortXmitData: data transmitted, raw /sys value (mlx5 reports octets/4). Use rate()."
emit_counter counters port_rcv_data \
  "PortRcvData: data received, raw /sys value (mlx5 reports octets/4). Use rate()."
emit_counter counters port_xmit_wait \
  "PortXmitWait: ticks the port had data to send but no Tx credits (congestion; the 'slow, no error' tell)."
emit_counter counters symbol_error \
  "SymbolErrorCounter: minor link/PHY symbol errors."
emit_counter counters port_rcv_errors \
  "PortRcvErrors: packets received with an error."
emit_counter counters link_downed \
  "LinkDownedCounter: number of times the link failed and went DOWN."
emit_counter counters link_error_recovery \
  "LinkErrorRecoveryCounter: successful link error-recovery events."

# ---- HCA hw_counters — RC/QP retransmission (invisible to switch counters) ---
emit_counter hw_counters packet_seq_err \
  "packet sequence errors — RC retransmission trigger."
emit_counter hw_counters out_of_sequence \
  "out-of-sequence packets received on RC QPs."
emit_counter hw_counters implied_nak_seq_err \
  "implied NAK sequence errors."
emit_counter hw_counters local_ack_timeout_err \
  "local ACK timeout errors — RC timeouts add latency with ZERO switch error."
emit_counter hw_counters rnr_nak_retry_err \
  "receiver-not-ready NAK retry exhaustions."
emit_counter hw_counters req_cqe_error \
  "requester completion-queue (CQE) errors."
emit_counter hw_counters resp_cqe_error \
  "responder completion-queue (CQE) errors."
emit_counter hw_counters out_of_buffer \
  "receive out-of-buffer events (no RQ WQE available)."
emit_counter hw_counters duplicate_request \
  "duplicate RC requests received (peer retransmitted)."

# ---- port logical state (gauge; complements down/error counters) ------------
emit "# HELP ib_hca_port_state IB port-1 logical state code (4=ACTIVE, 3=ARMED, 2=INIT, 1=DOWN)."
emit "# TYPE ib_hca_port_state gauge"
if (( ${#IB_HCAS[@]} )); then
  for hca in "${IB_HCAS[@]}"; do
    st_file="${IB_SYS_DIR}/${hca}/ports/1/state"
    [ -r "$st_file" ] || continue
    raw=""
    IFS= read -r raw < "$st_file" 2>/dev/null           # e.g. "4: ACTIVE"; gate
    code="${raw%%:*}"; code="${code// /}"                # on value, not read status
    case "$code" in ''|*[!0-9]*) continue ;; esac
    emit "ib_hca_port_state{hca=\"${hca}\"} ${code}"
  done
fi

# ---- publish atomically (torn/partial .prom drops the WHOLE file in scrape) --
chmod 0644 "$TMP" 2>/dev/null || true
mv -f "$TMP" "$OUT"
trap - EXIT
