# NVLMapper — switch-side intra-rack NVLink down-lane mapping

`nvlmapper` surfaces **which intra-rack NVLink lanes are DOWN**, seen from the *switch
side* of each NVLink rack rather than from the hosts. It is the pre-flight "is the NVLink
fabric inside this rack whole?" signal: an operator (or a job launcher) can gate on
`count(nvl_lane_down{rack=~"<job racks>"}) == 0` before scheduling onto a rack.

```
 NVSwitch tray BMCs (per rack)                  ObsVM (this container)
 ┌──────────────┐                     sweep     ┌───────────────────────────┐   scrape
 │ SW1 BMC      │◀──── vendor ────────────────  │ run_nvlmapper.sh          │  ┌────────────┐
 │ SW2 BMC      │      NVLMapper binary          │   → midfile per rack      │  │ Prometheus │
 │  ...         │      (config.yml: IPs+creds)   │ parse_nvlmapper.py        │  │    / VM    │
 └──────────────┘                                │   → nvl_lane_down*        │  └────────────┘
                                                 │ nvlmapper_exporter.py :9120│◀────────
                                                 └───────────────────────────┘
```

## The vendor binary is NOT in this repo

`NVLMapper` is **vendor-supplied**. It is not distributed here and must be obtained from your
hardware vendor and dropped into `nvlmapper/bin/` (gitignored). This repo ships only the
*integration*: the config template, the sweep wrapper, the parser, the exporter, and the
compose service. Everything site-specific is an environment variable or a `config.yml` value —
nothing is hardcoded and no secret is ever committed.

## Files

| File | Role |
|---|---|
| `config.yml.template` | Copy to `config.yml` (gitignored) and fill in per-rack NVSwitch tray BMC IPs, admin creds, `cluster`, and `flavor`. Placeholders only — no real values. |
| `run_nvlmapper.sh` | Reads `config.yml`, invokes the vendor binary against each rack's tray BMCs, writes `midfile__acp_down_connections.txt` per rack under the output root. Password is passed via env, never on argv. |
| `parse_nvlmapper.py` | Parses the midfiles into Prometheus gauges. Stdlib-only; `collect()` never raises (torn/partial NFS files are skipped). |
| `nvlmapper_exporter.py` | HTTP exporter on `:9120` (compose default; script default 9116 — 9116 on the obs host belongs to ib-cable-issue). Parses on a fast cadence, optionally runs the sweep on a slow cadence, and can also write a node_exporter textfile. Emits liveness so "blank != healthy". |
| `docker-compose.nvlmapper.yml` | Compose fragment for the `nvlmapper-exporter` service (mirrors the example `bcm-exporter`). |
| `test-samples/` | Synthetic midfile for validating the parser (`python3 parse_nvlmapper.py test-samples`). |

## Metrics

| Metric | Labels | Meaning |
|---|---|---|
| `nvl_lane_down` | `cluster,[flavor,]rack,tray,acp_port,cartridge,cage,lane` | **Present only when a lane is DOWN**, value `1`. Example input `SW1 acp53 connects to CC1_C5_Lane_15` → `tray=SW1 acp_port=acp53 cartridge=CC1 cage=C5 lane=15`. |
| `nvl_lane_down_total` | `cluster,[flavor,]rack` | Count of down lanes on the rack; **`0` == healthy** pre-flight gate. Gauge despite the `_total` name. |
| `nvlmapper_source_mtime_seconds` | `cluster,[flavor,]rack` | Max mtime of the rack's parsed midfile(s). The freshness heartbeat that proves *blank ≠ healthy*. |
| `nvlmapper_exporter_up` / `nvlmapper_scrape_duration_seconds` / `nvlmapper_last_scan_timestamp_seconds` / `nvlmapper_collector_errors_total` | `cluster,[flavor]` | Exporter liveness. |
| `nvlmapper_sweep_enabled` / `nvlmapper_sweep_last_timestamp_seconds` / `nvlmapper_sweep_last_rc` | `cluster,[flavor]` | Vendor-sweep status (only meaningful when `NVLMAPPER_RUN_BINARY=true`). |

> **Gate correctly.** `nvl_lane_down` is present-only-when-down, so an *empty* query result is
> ambiguous (healthy **or** no data). Always pair the gate with the heartbeat:
> `count(nvl_lane_down{rack=~"$racks"}) == 0` **AND** `nvlmapper_source_mtime_seconds` is fresh.
> A midfile whose rack can't be determined lands under `rack="unknown"`; those lanes are invisible
> to a rack-scoped gate, so also guard on `sum(nvl_lane_down_total) > 0` (the parser logs a WARN).

## Configure & run

1. **Get the binary** from your vendor and place it at `nvlmapper/bin/nvlmapper` (or set
   `NVLMAPPER_BIN_NAME`).
2. **Render config:** `cp config.yml.template config.yml`, fill in racks/trays/creds, then
   `chmod 0600 config.yml`. It is gitignored.
3. **Set env** (the installer interview writes these to `.env`):

   | Variable | Purpose | Secret |
   |---|---|---|
   | `NVLMAPPER_ENABLED` | Master on/off for the service | no |
   | `NVLINK_FLAVOR` | `flavor=` label (e.g. `nvl72`) | no |
   | `CLUSTER_NAME` | `cluster=` label (shared, stack-wide) | no |
   | `NVLMAPPER_RUN_BINARY` | Let the container run the vendor sweep | no |
   | `NVLMAPPER_BMC_USER` | Fallback tray BMC admin user | no |
   | `NVLMAPPER_BMC_PASSWORD` | Fallback tray BMC admin password | **yes** |
   | `NVLMAPPER_BIN_DIR` / `NVLMAPPER_OUTPUT_DIR` | Host mounts for the binary and midfile output | no |
   | `NVLMAPPER_PORT` / `NVLMAPPER_SCRAPE_INTERVAL` / `NVLMAPPER_RUN_INTERVAL` | Port and cadences | no |

4. **Bring it up:**
   ```bash
   docker compose -f docker-compose.yml -f nvlmapper/docker-compose.nvlmapper.yml up -d nvlmapper-exporter
   curl -s localhost:9120/metrics | grep -E 'nvlmapper_exporter_up|nvl_lane_down_total'
   ```
5. **Adjust the binary flags.** `NVLMAPPER_BIN_ARGS` in `run_nvlmapper.sh` is a placeholder argv
   template (`--rack {rack} --bmc-hosts {bmc_hosts} --username {bmc_user} --output {midfile}`).
   Match it to your vendor build's real flags. The password is exported to the child as
   `NVLMAPPER_BMC_PASSWORD` / `BMC_PASSWORD`, never placed on the command line.

## Two collection modes

- **Sweep here** (`NVLMAPPER_RUN_BINARY=true`): the container runs the binary every
  `NVLMAPPER_RUN_INTERVAL` seconds and parses the fresh midfiles. Needs the binary mounted and
  routable BMC access.
- **Land externally** (`NVLMAPPER_RUN_BINARY=false`, the default): some other job runs the binary
  and drops midfiles into `NVLMAPPER_OUTPUT_DIR/nvlmapper/<RACK>/`; this container only parses and
  serves. No BMC access or binary required in the container.

## Quick parser check

```bash
python3 parse_nvlmapper.py test-samples
# -> nvl_lane_down{cluster="cluster",rack="R0001",tray="SW1",acp_port="acp53",...} 1
#    nvl_lane_down_total{cluster="cluster",rack="R0001"} 2
```
