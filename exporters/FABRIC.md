# InfiniBand Fabric Exporters

Seven exporters that turn an NVIDIA InfiniBand fabric (managed by UFM + NVOS
switches) into Prometheus metrics for the NVIS observability stack. Each is a
stock `python:3.11-slim` container with its script mounted from this directory
(mirrors the `bcm-exporter` pattern); all site values come from the `.env` the
installer writes — nothing is hardcoded.

| Exporter | Dir | Port | Source | Needs |
|---|---|---|---|---|
| UFM REST | `ufm/` | 9103 | UFM REST API | `UFM_HOST/USER/PASSWORD` |
| UFM Telemetry | `ufm-telemetry/` | 9105 | UFM Telemetry CSV (`:9001`) | `UFM_HOST` |
| IB LLDP / SM redundancy | `ib-lldp/` | 9114 | UFM REST + switch SSH sweep | `UFM_*`, `SWITCH_SSH_*` |
| IB cable audit (scanner) | `cable-audit/` | — | UFM SM link view **or** switch SSH | `UFM_*` or `SWITCH_SSH_*` + topology artifacts |
| IB cable-issue | `ib-cable-issue/` | 9116 | cable-audit CSV output | scanner output volume |
| IB switch audit / firmware | `ib-switch-audit/` | 9117 | inventory + audit CSVs | CSV artifacts |
| IB identity / topology bridge | `ib-identity/` | 9118 | topology + placement + inventory | topology artifacts |

## Run

```bash
cp .env.fabric.example .env        # then fill in UFM + switch creds
docker compose -f docker-compose.yml -f docker-compose.fabric.yml up -d
```

Append the jobs in `exporters/prometheus/prometheus-fabric.yml` to your
`prometheus/prometheus.yml` so Prometheus scrapes the fabric exporters.

## Environment variables

See `.env.fabric.example` for the full annotated list. The key ones:

- `UFM_HOST`, `UFM_USER`, `UFM_PASSWORD` — UFM REST API (required for `ufm`,
  `ufm-telemetry`, `ib-lldp`, and cable-audit's default `ufm` scan mode).
- `SWITCH_SSH_USER`, `SWITCH_SSH_PASSWORD` — NVOS switch SSH, used by the
  `ib-lldp` transceiver sweep and cable-audit's `ssh` scan mode.
- `SWITCH_LIST` — optional `name=ip,name=ip,...` override; empty means `ib-lldp`
  auto-discovers the switch IPs from UFM.
- `CLUSTER_NAME`, `FABRIC` — labels stamped on every series.

## Ground-truth artifacts (cable-audit, ib-switch-audit, ib-identity)

Three exporters read site-specific ground-truth files (expected cabling map,
host placement, switch inventory, SSH audit CSVs). This repo ships **tiny
sanitized SAMPLE files** under each exporter's `data/` directory so the stack
runs out of the box; replace them with your site's real artifacts to emit real
data. Each of those three directories has its own `README.md` describing the
file schemas and how to generate them.

## Metric families (prefixes)

- `ufm_*` — fabric/switch/port/link/alarm/event state + counters (UFM REST).
- `ufm_telemetry_*`, `phy_*`, `eff_ber`, `ufm_port_*` — per-port PHY/BER/FEC.
- `ibl_*` — SM master/standby redundancy, pkeys, fabric health score, switch
  SSH reachability + transceiver counts.
- `ib_cable_*` — miswiring issues, unreachable switches, repatch worklist,
  run-over-run new/resolved deltas.
- `ib_switch_audit_*`, `ib_switch_*` — switch identity/firmware/software drift,
  hostname mismatch, SSH reachability totals.
- `ib_switch_info`, `ib_link_peer`, `ib_host_rail_leaf`, `ib_leaf_spine`,
  `ib_leaf_leaf`, `ib_node_placement`, `ib_identity_*` — the topology/identity
  join bridge (value=1 info-metrics for label joins + blast-radius).

## Prerequisite checks (installer interview)

- **UFM REST reachable + authenticated**:
  `curl -sk -u "$UFM_USER:$UFM_PASSWORD" https://$UFM_HOST/ufmRest/app/ufm_version`
  should return JSON with a version.
- **UFM Telemetry endpoint**:
  `curl -s http://$UFM_HOST:${UFM_TELEMETRY_PORT:-9001}/csv/metrics | head -1`
  should return a CSV header row.
- **Switch SSH reachable + creds** (only for `ib-lldp` sweep / cable-audit ssh
  mode): `sshpass -p "$SWITCH_SSH_PASSWORD" ssh -o StrictHostKeyChecking=no
  "$SWITCH_SSH_USER@<switch-ip>" 'nv show system'` should connect.
