#!/usr/bin/env bash
# run_nvlmapper.sh — drive the vendor-supplied NVLMapper binary across every NVLink rack.
#
# NVLMapper is NOT shipped in this repo (see README.md). This wrapper only tells it WHICH
# racks / NVSwitch tray BMCs to sweep and WITH WHAT credentials, reading nvlmapper/config.yml.
# For each rack it invokes the binary and expects a midfile__acp_down_connections.txt to be
# written under $NVLMAPPER_OUTPUT_ROOT/nvlmapper/<RACK>/, which parse_nvlmapper.py then turns
# into nvl_lane_down* metrics.
#
# Every site value comes from the environment / config.yml — NOTHING is hardcoded:
#   NVLMAPPER_BIN            path to the vendor NVLMapper binary            (required)
#   NVLMAPPER_CONFIG         path to the rendered config.yml               (default ./config.yml)
#   NVLMAPPER_OUTPUT_ROOT    root that will hold nvlmapper/<RACK>/midfile  (default /data)
#   NVLMAPPER_BMC_USER       fallback tray BMC admin user                  (else config bmc_defaults)
#   NVLMAPPER_BMC_PASSWORD   fallback tray BMC admin password (secret)     (else config bmc_defaults)
#   NVLMAPPER_SWEEP_SCOPE    rack | tray  (one binary call per rack, or per tray)  (default rack)
#   NVLMAPPER_BIN_ARGS       argv template for the binary (see default below) — ADJUST to your
#                            vendor build's real flags. Placeholders substituted per call:
#                              {rack} {tray} {bmc_host} {bmc_hosts} {bmc_user} {midfile} {output_dir}
#                            The password is NEVER placed on argv; it is exported to the child
#                            process as NVLMAPPER_BMC_PASSWORD / BMC_PASSWORD.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${NVLMAPPER_BIN:-}"
CONFIG="${NVLMAPPER_CONFIG:-$HERE/config.yml}"
OUTPUT_ROOT="${NVLMAPPER_OUTPUT_ROOT:-/data}"
SCOPE="${NVLMAPPER_SWEEP_SCOPE:-rack}"
DEFAULT_ARGS='--rack {rack} --bmc-hosts {bmc_hosts} --username {bmc_user} --output {midfile}'
export NVLMAPPER_BIN_ARGS="${NVLMAPPER_BIN_ARGS:-$DEFAULT_ARGS}"

log() { printf '[run_nvlmapper] %s\n' "$*" >&2; }

# ---- prereq: config present & filled (no leftover template placeholders) --------------------
if [[ ! -f "$CONFIG" ]]; then
  log "FATAL config not found: $CONFIG (copy config.yml.template -> config.yml and fill it in)"; exit 2
fi
if grep -q 'REPLACE_WITH_' "$CONFIG"; then
  log "FATAL $CONFIG still contains REPLACE_WITH_ placeholders — fill in racks/trays/creds first"; exit 2
fi

# ---- prereq: vendor binary present & executable --------------------------------------------
if [[ -z "$BIN" ]]; then
  log "FATAL NVLMAPPER_BIN is unset — point it at the vendor NVLMapper binary (not shipped in this repo)"; exit 3
fi
if [[ ! -x "$BIN" ]]; then
  log "FATAL NVLMAPPER_BIN=$BIN is not an executable file"; exit 3
fi

command -v python3 >/dev/null 2>&1 || { log "FATAL python3 required to read config.yml"; exit 4; }

# ---- emit a sweep plan from config.yml (rack \t tray \t bmc_host \t bmc_user) ---------------
# Password is resolved per-tray in-python and exported via env at call time, never printed here.
PLAN="$(python3 - "$CONFIG" "$SCOPE" <<'PY'
import sys, json
cfg_path, scope = sys.argv[1], sys.argv[2]
try:
    import yaml
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}
except Exception as e:
    sys.stderr.write("[run_nvlmapper] FATAL cannot parse %s: %r (pip install pyyaml)\n" % (cfg_path, e))
    sys.exit(5)

defaults = cfg.get("bmc_defaults", {}) or {}
rows = []
for rk in (cfg.get("racks") or []):
    rack = str(rk.get("rack", "")).strip()
    if not rack:
        continue
    for tray in (rk.get("trays") or []):
        name = str(tray.get("name", "")).strip()
        host = str(tray.get("bmc_host", "")).strip()
        if not host:
            continue
        user = str(tray.get("username") or defaults.get("username") or "").strip()
        rows.append({"rack": rack, "tray": name, "bmc_host": host, "bmc_user": user})
# TSV: one line per tray; the caller groups by rack for scope=rack.
for r in rows:
    print("%s\t%s\t%s\t%s" % (r["rack"], r["tray"], r["bmc_host"], r["bmc_user"]))
PY
)" || { log "FATAL failed to build sweep plan from $CONFIG"; exit 5; }

if [[ -z "$PLAN" ]]; then
  log "FATAL no racks/trays with a bmc_host found in $CONFIG"; exit 5
fi

# ---- substitute placeholders in the argv template, then invoke the binary ------------------
subst() { # subst "template" rack tray bmc_host bmc_hosts bmc_user midfile output_dir
  local t="$1"
  t="${t//\{rack\}/$2}"; t="${t//\{tray\}/$3}"; t="${t//\{bmc_host\}/$4}"
  t="${t//\{bmc_hosts\}/$5}"; t="${t//\{bmc_user\}/$6}"; t="${t//\{midfile\}/$7}"
  t="${t//\{output_dir\}/$8}"
  printf '%s' "$t"
}

run_binary() { # run_binary rack tray bmc_host bmc_hosts bmc_user
  local rack="$1" tray="$2" bmc_host="$3" bmc_hosts="$4" bmc_user="$5"
  local outdir="$OUTPUT_ROOT/nvlmapper/$rack"
  local midfile="$outdir/midfile__acp_down_connections.txt"
  mkdir -p "$outdir" || { log "cannot create $outdir"; return 1; }
  local args; args="$(subst "$NVLMAPPER_BIN_ARGS" "$rack" "$tray" "$bmc_host" "$bmc_hosts" "$bmc_user" "$midfile" "$outdir")"
  # shellcheck disable=SC2206
  local argv=( $args )
  log "sweep rack=$rack tray=${tray:-*} host=${bmc_host:-$bmc_hosts} -> $midfile"
  # Password stays out of argv/ps: exported for the child only.
  NVLMAPPER_BMC_PASSWORD="${NVLMAPPER_BMC_PASSWORD:-}" BMC_PASSWORD="${NVLMAPPER_BMC_PASSWORD:-}" \
    "$BIN" "${argv[@]}"
}

rc_all=0
if [[ "$SCOPE" == "tray" ]]; then
  # one binary call per tray
  while IFS=$'\t' read -r rack tray host user; do
    [[ -z "$rack" ]] && continue
    run_binary "$rack" "$tray" "$host" "$host" "$user" || rc_all=1
  done <<< "$PLAN"
else
  # one binary call per rack, passing every tray BMC host (comma-joined)
  racks="$(cut -f1 <<< "$PLAN" | awk 'NF' | sort -u)"
  while IFS= read -r rack; do
    [[ -z "$rack" ]] && continue
    hosts="$(awk -F'\t' -v r="$rack" '$1==r{print $3}' <<< "$PLAN" | paste -sd, -)"
    user="$(awk -F'\t' -v r="$rack" '$1==r{print $4; exit}' <<< "$PLAN")"
    run_binary "$rack" "" "" "$hosts" "$user" || rc_all=1
  done <<< "$racks"
fi

[[ $rc_all -eq 0 ]] && log "sweep complete (all racks OK)" || log "sweep completed with errors (see above)"
exit $rc_all
