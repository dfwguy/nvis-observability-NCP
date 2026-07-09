# Redfish BMC Exporter

A single central poller that talks to every node's **BMC** over the Redfish API
and exports hardware telemetry as Prometheus metrics on **:9104**. Nodes run no
agent for this — one process scrapes the whole cluster with bounded concurrency.

Pure Python standard library — no pip dependencies.

## What it collects (per node)

| Area | Metrics |
|------|---------|
| Reachability | `redfish_up{bmc,node}` |
| Identity | `redfish_system_info{model,serial,uuid,bios_version,power_state,...}` |
| Boot order | `redfish_boot_order_entry{entry}` (detect PXE/disk misconfig) |
| Firmware | `redfish_firmware_info{component,version}`, `redfish_firmware_drift`, `redfish_firmware_baseline_version` |
| Cooling | `redfish_fan_rpm`, `redfish_fan_health`, `redfish_temperature_celsius{sensor}` |
| Power | `redfish_power_watts`, `redfish_component_power_watts{component}` |
| Leak (liquid-cooled) | `rf_leakdetector_state{sensor,state}` (+ legacy alias series) |
| SEL / event log | `redfish_sel_severity_count{severity}`, `redfish_sel_latest_timestamp`; entries also written to a JSONL file for Loki |

Every series carries `cluster` and `node` labels.

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `BMC_TARGETS` | — | Path to a target file (`node,ip` / `ip` / `ip/cidr` per line). See `bmc-targets.example.csv`. |
| `BMC_HOSTS` | — | Fallback: inline comma-separated IPs/CIDRs (node = ip). |
| `BMC_USER` | `root` | BMC username. |
| `BMC_PASSWORD` | *(req)* | BMC password. **Secret — env only.** (`BMC_PASS` accepted as alias.) |
| `BMC_CONCURRENCY` | `16` | BMCs polled in parallel. |
| `SENSOR_CONCURRENCY` | `8` | Redfish requests per BMC (keep ≤ 8 — BMC HTTP servers cap connections). |
| `CLUSTER_NAME` | `""` | `cluster` label on every metric. |
| `DGX_FLAVOR` | — | Platform hint for firmware-drift baseline when the BMC reports no Model (`DGXB300`/`DGXB200`/`DGXH100`/`DGXA100`). |
| `PORT` | `9104` | Listen port. |
| `SCRAPE_INTERVAL` | `60` | Seconds between full polls. |
| `VERIFY_SSL` | `false` | Verify BMC TLS cert (BMC certs are usually self-signed). |
| `BMC_TIMEOUT` | `10` | Per-request timeout (seconds). |
| `FW_DISABLED` | `false` | Skip firmware inventory (much faster). |
| `SEL_DISABLED` | `false` | Skip SEL / event-log collection. |
| `SENSOR_NAME_REGEX` | *(built-in)* | Sensor-basename allow-list; empty string keeps every sensor. |
| `SEL_LOG_PATH` | `/var/log/nvis/redfish-sel.json` | JSONL sink for SEL entries (tail with Alloy/Promtail → Loki). |

## Run

```bash
# standalone
BMC_TARGETS=./secrets/bmc-targets.csv BMC_USER=root BMC_PASSWORD='***' \
  CLUSTER_NAME=my-cluster python3 redfish_exporter.py
curl -s localhost:9104/metrics | grep redfish_

# docker-compose (from repo root)
docker compose -f docker-compose.yml -f exporters/compose.bmc-bcm-netq.yml up -d redfish-bmc-exporter
```

## Endpoints

* `/metrics` — Prometheus scrape
* `/-/healthy` (`/health`) — liveness
* `/refresh` (`?bmc=<ip>` optional) — force an immediate re-poll (e.g. after a firmware update)

## Targets file

The real BMC inventory is **never committed**. The installer writes it to
`./secrets/bmc-targets.csv` (gitignored) and the compose service mounts it. Use
`bmc-targets.example.csv` as the template.
