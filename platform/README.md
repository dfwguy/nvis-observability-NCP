# NVIS Appliance — Central Stack (`platform/`)

The single-host observability control plane for a DGX SuperPOD cluster. Five
containers, all env-driven, no hardcoded site values or secrets.

| Service | Image | Port | Role |
|---|---|---|---|
| `victoriametrics` | victoria-metrics:v1.103.0 | 8428 | Long-term metrics TSDB (system of record) |
| `prometheus` | prometheus:v2.54.1 | 9090 | Short-term scrape + remote_write fan-in → VM |
| `loki` | loki:3.1.0 | 3100 | Log store |
| `grafana` | grafana:11.1.4 | 3000 | Dashboards (VictoriaMetrics = default datasource) |
| `alloy` | alloy:v1.5.1 | 12345 / 9099 / 9098 | Ingest gateway + self-observability |

## Data flow

```
 GPU-node Alloy agents ─remote_write→ alloy:9099 ─→ prometheus ─remote_write→ victoriametrics
 site exporters (host net) ───────scrape──────────→ prometheus ─────────────→ victoriametrics
 node/app logs ─push→ alloy:9098 ─→ loki
 grafana ──queries──→ victoriametrics (default) · prometheus (recent) · loki (logs)
```

Prometheus keeps a 15-day buffer; VictoriaMetrics holds `DATA_RETENTION_MONTHS`
of history and is the default Grafana datasource for long-range dashboards.

## Run it

```bash
cp .env.example .env         # or let installer/install.sh write it
# edit .env: CLUSTER_NAME, GRAFANA_ADMIN_PASSWORD, DATA_RETENTION_MONTHS, DGX_FLAVOR
docker compose --env-file .env up -d
```

Grafana → http://<this-host>:3000 (admin / `$GRAFANA_ADMIN_PASSWORD`).

## Environment (`.env`)

| Var | Secret | Purpose |
|---|---|---|
| `CLUSTER_NAME` | no | `cluster` label on all series/logs; dashboard scope |
| `DGX_FLAVOR` | no | `B300\|B200\|B100\|H200\|H100\|A100\|custom` |
| `GRAFANA_ADMIN_PASSWORD` | **yes** | Grafana admin password (first boot) |
| `DATA_RETENTION_MONTHS` | no | VictoriaMetrics retention |
| `EXPORTER_HOST` | no | Exporter host as seen from Prometheus (default `host.docker.internal`) |
| `PROM_SCRAPE_INTERVAL` | no | Global scrape interval (template render) |
| `VM_REMOTE_WRITE_URL` | no | VictoriaMetrics remote_write URL (template render) |

## Files

- `docker-compose.yml` — the five central services.
- `prometheus/prometheus.yml` — working scrape config (exporters via host-gateway) + remote_write to VM.
- `prometheus/prometheus.yml.tmpl` — env-templated form the installer renders with `envsubst`.
- `loki/loki-config.yaml` — single-binary Loki, filesystem store, retention guards.
- `alloy/config.alloy` — central ingest gateway + self-scrape (per-node template lives with node-agents).
- `grafana/provisioning/datasources/datasources.yaml` — VM (default) + Prometheus + Loki.
- `grafana/provisioning/dashboards/dashboards.yaml` — file provider → `NVIS` folder.
- `grafana/dashboards/` — dashboard JSON is dropped here by the dashboards component.

## Exporters

Site exporters (BCM, Slurm, UFM, Redfish, IB fabric, job/benchmark) are their
own component and run with `network_mode: host` on this box. Prometheus scrapes
them via `host.docker.internal:<port>`; edit `prometheus/prometheus.yml` to
comment out any exporter you don't run. Port map: BCM 9401, Slurm 9402, Redfish
9104, UFM 9103, UFM-telemetry 9105, IB-LLDP 9114, IB-cable-issue 9116,
IB-identity 9118, IB-switch-audit 9117, cable-audit 9107, NCCL-artifact 9115,
LLMB-job 9119.

Secrets live only in `.env` (gitignored). Nothing here is committed with a
credential or a site IP.
