# NVIS Observability Appliance — Architecture

Turnkey observability for NVIDIA DGX / SuperPOD-class GPU clusters. One
observability host runs the whole control plane in docker compose; compute
nodes run four native systemd agents; a set of central "poller" exporters
talks to the site's management planes (BCM, UFM, BMCs, NetQ, Slurm). Every
site value is an environment variable written by the interview installer
(`installer/install.sh`) into gitignored `.env` files — nothing in this repo
is site-specific.

```
                 ┌──────────────────────── GPU compute nodes (systemd, no containers) ─────────┐
                 │ dcgm-exporter:9400   node_exporter:9100(+textfile)   cgroup_exporter:9306    │
                 │                     └── Grafana Alloy (scrape + journal/log tail) ──┘        │
                 └───────────────┬──────────────────────────────────┬──────────────────────────┘
                     remote_write│(19090)                  loki push│(13100)
                 ┌───────────────▼──────────────────────────────────▼──────────────────────────┐
                 │        HEAD NODE — nginx L4 stream relay (node-agents/head-node/)           │
                 │        19090 → obs-host:9099              13100 → obs-host:9098             │
                 └───────────────┬──────────────────────────────────┬──────────────────────────┘
                                 │                                  │
   management planes             ▼                                  ▼
  ┌──────────────┐   ┌────────────────────────────────────────────────────────────────────────┐
  │ BMCs (Redfish)│   │            OBSERVABILITY HOST — platform/docker-compose.yml            │
  │ BCM head node │◄──┤  alloy:9099/9098 (ingest gateway)      loki:3100 (logs)                │
  │ UFM REST/CSV  │   │        │ remote_write                                                  │
  │ IB switch SSH │   │  prometheus:9090 ──remote_write──► victoriametrics:8428 (long-term)    │
  │ NetQ API      │   │        ▲ scrape (host-gateway)                                         │
  │ Slurm ctl     │   │  grafana:3000 ── queries VM (default) · Prometheus (recent) · Loki     │
  │ shared-FS logs│   ├────────────────────────────────────────────────────────────────────────┤
  └──────▲───────┘   │  site exporters (compose fragments, network_mode: host, ports below)    │
         └────────────┤  docker-compose.fabric.yml · exporters/compose.* · nvlmapper/          │
                      └────────────────────────────────────────────────────────────────────────┘
```

(Example addresses in this document use the RFC 5737 documentation range,
e.g. `198.51.100.10` — substitute your own.)

## 1. Central platform (`platform/`)

Five containers, one `docker compose up`, all env-driven
(`platform/.env.example`):

| Service | Image | Port | Role |
|---|---|---|---|
| `victoriametrics` | victoria-metrics v1.103.0 | 8428 | Long-term metrics TSDB — the system of record (`DATA_RETENTION_MONTHS`, default 12) |
| `prometheus` | prometheus v2.54.1 | 9090 | Scrapes the site exporters + receives node remote_write; 15-day local buffer; remote_writes everything to VictoriaMetrics |
| `loki` | loki 3.1.0 | 3100 | Log store (node journals, Slurm/job logs, exporter events) |
| `grafana` | grafana 11.1.4 | 3000 | Dashboards; VictoriaMetrics is the default datasource |
| `alloy` | alloy v1.5.1 | 12345 / 9099 / 9098 | Ingest gateway: node agents remote_write metrics to :9099 and push logs to :9098; also self-scrapes the central stack |

- `prometheus/prometheus.yml.tmpl` is rendered by the installer (envsubst) so
  the `cluster` external label and exporter host come from `.env`.
- Datasources are provisioned with fixed UIDs (`nvis-victoriametrics`,
  `nvis-prometheus`, `nvis-loki`) that every dashboard references — do not
  rename them.
- Exporters run with `network_mode: host` on the same box; Prometheus reaches
  them at `host.docker.internal:<port>` via the docker host-gateway.

## 2. Node agents & data path (`node-agents/`)

Per-node agents are **native systemd units** (no container runtime needed on
compute nodes), staged from a cluster share and rolled out over SSH
(`stage-binaries.sh` → `rollout.sh` → `install-node.sh`, idempotent and
reversible).

| Agent | Port | Purpose |
|---|---|---|
| dcgm-exporter | 9400 | Per-GPU metrics via the local `nv-hostengine` |
| node_exporter | 9100 | Host metrics + textfile collectors (`.prom`) |
| cgroup_exporter | 9306 | Per-Slurm-job cpu/mem/io keyed by `jobid` |
| Grafana Alloy | 12345 | Scrapes the three above + self, tails logs/journal, stamps `cluster`/`dgx_flavor`/`node` labels, pushes upstream |

Two textfile collectors (systemd timers) add signals no stock exporter has:
GPU-side NVLink per-link errors (`host_nvlink_collector.sh`) and IB HCA verbs
counters (`hca_verbs_collector.sh`) — the "slow but no switch error"
retransmission signal.

**Why the head-node relay?** Compute nodes typically live on an isolated
compute/management VLAN and cannot reach the observability host; only the
head/BCM node can. The head node runs a pass-through **nginx `stream` (L4)
proxy** (`node-agents/head-node/nginx-stream-relay.conf.template`, deployable
via docker or host nginx with `setup-relay.sh`):

- node Alloy `remote_write` → `RELAY_HOST:19090` → obs host Alloy `:9099` → Prometheus → VictoriaMetrics
- node Alloy Loki push → `RELAY_HOST:13100` → obs host Alloy `:9098` → Loki

Alloy's remote_write is WAL-buffered, so relay or obs-host restarts do not
lose node metrics. Sites where nodes can reach the obs host directly simply
set `RELAY_HOST` to it and skip the relay.

## 3. Site exporters (`exporters/`, `docker-compose.fabric.yml`)

Twelve central pollers — small stdlib-only Python scripts, each run as a
`python:3.11-slim` compose service with the script bind-mounted (no custom
images to build or trust). All credentials arrive as env vars from the
gitignored `.env`; the BMC target list lives in gitignored `./secrets/`.

| Exporter | Port | Talks to | Gives you |
|---|---|---|---|
| `redfish-bmc` | 9104 | node BMCs (Redfish) | temps, power, fans, PSUs, System Event Log |
| `bcm` | 9401 | BCM head node (SSH, key-only) | cluster inventory, node status/health |
| `netq` | 9109 | NVIDIA NetQ API (optional) | Ethernet/Cumulus fabric state |
| `ufm` | 9103 | UFM REST | IB fabric/switch/port/link/alarm state (`ufm_*`) |
| `ufm-telemetry` | 9105 | UFM Telemetry CSV (:9001) | per-switch-port PHY counters, BER, FEC histograms |
| `ib-lldp` | 9114 | UFM REST + switch SSH sweep | LLDP neighbor truth, transceiver detail, SM redundancy |
| `cable-audit` | — (scanner) | UFM (or per-switch SSH) | periodic expected-vs-actual cabling audit → CSVs |
| `ib-cable-issue` | 9116 | cable-audit CSVs | miscabling/issue metrics + optional Loki event lines |
| `ib-switch-audit` | 9117 | audit CSVs | switch hostname/SSH/resolution consistency |
| `ib-identity` | 9118 | expected topology + placement map | node ⇄ SU/rack/unit identity joins for dashboards |
| `llmb-job` | 9119 | per-job training stdout on the shared FS | per-Slurm-job step progress, TFLOPs, stall & NaN detection |
| `slurm-job` | 9402 | `squeue`/`sacct` (local or SSH) | `slurm_job_info` + the **`slurm_job_node`** jobid→node bridge |

The job pair is the differentiator: `slurm_job_node` joins a JobID to per-node
GPU/fabric series, while `llmb_job_*` says whether that job is training,
stalled, or diverging — even when the Slurm accounting row is empty.

Compose fragments merge from the repo root, all reading the same root `.env`:

```bash
docker compose --project-directory . \
  -f docker-compose.fabric.yml \
  -f exporters/compose.bmc-bcm-netq.yml \
  -f exporters/compose.job-monitor.yaml up -d
```

## 4. NVLMapper (`nvlmapper/`)

Optional switch-side intra-rack NVLink down-lane mapper for NVL-rack systems.
A vendor binary (NOT shipped here) logs into each rack's NVSwitch tray BMCs
and writes one `midfile__acp_down_connections.txt` per rack;
`parse_nvlmapper.py` + `nvlmapper_exporter.py` (**:9120**) turn those into
`nvl_lane_down_total{rack,tray,lane}`-style metrics with liveness (blank ≠
healthy). Rack/tray BMC inventory lives in gitignored `nvlmapper/config.yml`
(rendered from `config.yml.template`); the exporter stays inert unless
`NVLMAPPER_ENABLED=true`.

## 5. Dashboards (`platform/grafana/dashboards/`, `docs/DASHBOARD_INVENTORY.md`)

21 curated, generalized dashboards auto-provision into the **NVIS** Grafana
folder on boot (file provider, reloaded every 30 s). Highlights:

- **Job-centric** (the differentiators): `nvis-job-health` (L1 wallboard),
  `nvis-llmb-jobs`, `nvis-job-fabric-corr` (JobID → nodes → fabric health),
  `nvis-job-nccl-corr`, `nvis-job-logs`, `nvis-xid-errors`.
- **Fabric**: `nvis-net-home`, `nvis-ib-overview`, `nvis-ib-cable-audit`,
  `nvis-ib-offenders`.
- **Fleet/hardware**: `cluster-overview`, `gpu-health`, `node-deep-dive`,
  `bcm-cluster`, `nvis-hwval-*` (cluster health / GPU profiling / sensors).
- **Reporting**: `executive`, `business-insights`, `impact-analysis`,
  `nvis-home` (landing page).

All use `$datasource`/`$cluster` template variables — no hardcoded site
values. See `docs/DASHBOARD_INVENTORY.md` for the per-dashboard query
inventory.

## 6. Terraform (`terraform/`)

An alternative to the compose file-provider: provisions the three datasources
and every dashboard JSON into Grafana via the `grafana/grafana` provider.
Idempotent (`overwrite = true`); new dashboard files are picked up by
`for_each` on re-apply. State and `terraform.tfvars` are gitignored.

## 7. Security / no-leak posture

- **No secrets in git**: credentials exist only in gitignored, chmod-600
  `.env` files (written by the installer, held in memory during the
  interview), `./secrets/`, and `nvlmapper/config.yml`.
- **No custom images**: exporters run from stock `python:3.11-slim` with
  read-only bind mounts — auditable supply chain.
- **BCM auth is SSH-key only**; UFM/BMC/switch passwords are env-injected at
  container start, never baked into images or compose files.
- Sample data under `exporters/*/data/` and `nvlmapper/test-samples/` is
  synthetic (documentation-range IPs, `leaf01`/`gpu000001`-style names) and
  exists only to exercise the parsers.
