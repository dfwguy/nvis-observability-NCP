# IB cable audit scanner + ib-cable-issue exporter

`cable_audit_scan.py` discovers the **actual** per-port neighbors of every NVOS
switch, diffs them against the expected cabling map, and writes the
`cable-issue_*.csv` files that `ib-cable-issue-exporter` (`../ib-cable-issue`,
:9116) serves as `ib_cable_*` metrics. It is a **scanner**, not a long-running
server: the compose service runs it on `CABLE_AUDIT_INTERVAL` (default 12h).

## Scan modes (`SCAN_MODE`)

- `ufm` (default) — pull the UFM Subnet Manager's active-link view
  (`/ufmRest/resources/links`). The SM sees every ACTIVE IB link, so it avoids
  the LLDP blindness that turns live IB fabric ports into false
  "Media Unplugged". Needs `UFM_HOST/USER/PASSWORD`.
- `ssh` — legacy per-switch SSH + `NEIGHBOR_CMD` (lldp). For air-gapped / no-UFM
  sites. Needs `SWITCH_SSH_USER/SWITCH_SSH_PASSWORD` and switch mgmt IPs in the
  inventory CSV. Blind to IB fabric ports.

## Outputs (written to `OUTPUT_DIR`, default `/data`)

- `cable-issue_<UTC>.csv` — main snapshot (the exporter input).
- `cable-issue_<DATE>_Target.csv` — Wrong-neighbor + Wrong-port repatch worklist.
- `cable-issue-delta_<UTC>.csv` — run-over-run new/resolved change list.
- `cable_audit.prom` — freshness + change aggregates (textfile).

## Ground-truth artifacts (`data/`)

Sanitized SAMPLE files ship here; replace with your site's real ones.

| File | Schema |
|---|---|
| `expected-topology.json` | same file as the ib-identity exporter (see its README). |
| `switch_su_map.json` | `{"<switch>": {"su","role","rack","unit"}, ...}` — enrichment + the switch set to audit. |
| `ufm_switches_new.csv` | `ip,name,system_name,role,state,model,fw_version,sw_version,guid` — only needed for `ssh` mode (supplies mgmt IPs). |

Export `ufm_switches_new.csv` from UFM, e.g.:
```bash
curl -sk -u "$UFM_USER:$UFM_PASSWORD" \
  "https://$UFM_HOST/ufmRest/resources/systems?type=switch" \
  | jq -r '["ip","name","system_name","role","state","model","fw_version","sw_version","guid"],
           (.[] | [.ip,.guid,.system_name,.role,.state,.model,.fw_version,.sw_version,.guid])
           | @csv' > data/ufm_switches_new.csv
```

## Modes for testing (no live fabric)

```bash
# dry-run: diff a discovered-topology fixture against the expected map
OUTPUT_DIR=/tmp/out EXPECTED_TOPOLOGY=data/expected-topology.json \
  SWITCH_MAP_JSON=data/switch_su_map.json \
  python3 cable_audit_scan.py --dry-run --fixture <fixture.json>

# validate: reconstruct a fixture from a baseline CSV and assert the round-trip
python3 cable_audit_scan.py --validate --baseline <cable-issue_*.csv> \
  [--expect-switch <sw> --expect-total <n> --expect-su <SU> --expect-su-total <n>]
```

## Environment

`SCAN_MODE`, `UFM_HOST/USER/PASSWORD`, `UFM_VERIFY_SSL` (0/1),
`SWITCH_SSH_USER/SWITCH_SSH_PASSWORD`, `FABRIC`, `OUTPUT_DIR`,
`EXPECTED_TOPOLOGY`, `SWITCH_MAP_JSON`, `INVENTORY_CSV`, `EXCLUDE_EDGE_CLASSES`
(default `leaf-leaf`), `SWITCH_NAME_PREFIXES` (optional comma-separated
switch-hostname prefixes used as a fallback switch classifier in `ufm` mode;
empty by default — switch_map + UFM live systems are authoritative), `RETAIN`,
`CHANGE_CAP`, `NEIGHBOR_CMD` (ssh mode).

Runtime dep for `ssh` mode: `paramiko` (see `requirements.txt`); `ufm`/dry-run/
validate are stdlib-only.
