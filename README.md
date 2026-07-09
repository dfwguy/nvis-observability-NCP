# NVIS Observability Appliance (nvis-ova)

**Turnkey, deploy-anywhere observability for NVIDIA DGX / SuperPOD GPU clusters.**

One observability VM runs the full control plane in Docker Compose. Compute nodes run four native systemd agents. A suite of central pollers integrates with every management plane you have — BMC/Redfish, BCM, UFM, IB fabric switches, NetQ, Slurm, and training job logs — and surfaces everything through 21 curated Grafana dashboards.

No custom container images to build. No hardcoded site values. One interactive installer.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Directory Layout](#directory-layout)
- [Component Reference](#component-reference)
  - [1. Central Platform](#1-central-platform)
  - [2. Node Agents](#2-node-agents)
  - [3. Site Exporters](#3-site-exporters)
  - [4. NVLMapper](#4-nvlmapper)
  - [5. Dashboards](#5-dashboards)
  - [6. Terraform (alternative provisioning)](#6-terraform-alternative-provisioning)
- [Installer Deep-Dive](#installer-deep-dive)
- [Port Map](#port-map)
- [Secrets & Security Posture](#secrets--security-posture)
- [Upgrade & Re-run](#upgrade--re-run)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

---

## What It Does

| Capability | How |
|---|---|
| **Per-GPU metrics** (util, power, temp, XID, NVLink errors) | DCGM Exporter on every compute node |
| **Per-Slurm-job attribution** | `cgroup_exporter` + `slurm_job_node` bridge metric joins JobID to GPU/fabric series |
| **IB fabric health** (port BER, FEC, symbol errors, speed, link state) | UFM Telemetry + ib-lldp + ib-cable-issue pollers |
| **Cabling audit** | Automated expected-vs-actual cable scan — mismatches surfaced in Grafana |
| **BMC/hardware** (temps, power, PSUs, SEL events) | Redfish exporter per-BMC |
| **BCM cluster inventory** (node status, health, placement) | BCM SSH poller |
| **Training job progress** (step time, TFLOPs, stall/NaN detection) | llmb-job log scanner on shared FS |
| **NCCL Inspector correlation** | Optional NCCL Inspector `.prom` output ingested via textfile collector |
| **NVLink down-lane mapping** (NVL rack systems) | NVLMapper parser + exporter |
| **Log aggregation** (journal, Slurm logs, job stdout) | Grafana Alloy → Loki |
| **Long-term metrics retention** | VictoriaMetrics (default 12 months) |
| **Executive / reporting dashboards** | Business insights, impact analysis, cluster utilization |

---

## Architecture Overview

```
         ┌──────────── GPU compute nodes (systemd, no containers) ────────────┐
         │  dcgm-exporter:9400   node_exporter:9100   cgroup_exporter:9306    │
         │                 └── Grafana Alloy (scrape + log tail) ─┘           │
         └───────────────┬────────────────────────────┬───────────────────────┘
             remote_write│(19090)             loki push│(13100)
         ┌───────────────▼────────────────────────────▼───────────────────────┐
         │   HEAD NODE — nginx L4 stream relay (node-agents/head-node/)       │
         │   19090 → obs-host:9099              13100 → obs-host:9098         │
         └───────────────┬────────────────────────────┬───────────────────────┘
                         │                            │
  management planes      ▼                            ▼
 ┌─────────────────┐  ┌──────────────────────────────────────────────────────┐
 │ BMCs (Redfish)  │  │       OBSERVABILITY HOST — platform/docker-compose.yml│
 │ BCM head node   │◄─┤  alloy:9099/9098 (ingest)    loki:3100 (logs)        │
 │ UFM REST/CSV    │  │       │ remote_write                                  │
 │ IB switch SSH   │  │  prometheus:9090 ──► victoriametrics:8428 (long-term) │
 │ NetQ API        │  │       ▲ scrape (host-gateway)                         │
 │ Slurm squeue    │  │  grafana:3000 — queries VM · Prometheus · Loki        │
 │ shared-FS logs  │  ├──────────────────────────────────────────────────────┤
 └─────────────────┘  │ site exporters (compose fragments, network_mode: host)│
                      │ docker-compose.fabric.yml · compose.bmc-bcm-netq.yml  │
                      │ compose.job-monitor.yaml · nvlmapper/                 │
                      └──────────────────────────────────────────────────────┘
```

**Data flow summary:**

1. Each compute node runs four systemd agents (DCGM, node_exporter, cgroup_exporter, Grafana Alloy). Alloy scrapes the first three, tails journals and Slurm log files, and pushes metrics and logs upstream.
2. Compute nodes typically live on an isolated VLAN. The head/BCM node runs a pass-through nginx L4 relay that bridges metric and log traffic to the observability host without inspecting it.
3. On the observability host, Alloy forwards metrics into Prometheus (scrape + remote_write). Prometheus also scrapes all site exporters. Everything is remote_written into VictoriaMetrics for long-term storage.
4. Grafana queries VictoriaMetrics (primary), Prometheus (last 15 days), and Loki. All 21 dashboards use `$datasource` and `$cluster` template variables — no hardcoded site values anywhere.

---

## Prerequisites

### Observability host
- Linux (tested on Ubuntu 22.04 / RHEL 9), x86_64 or aarch64
- Docker Engine >= 24 with Compose v2 plugin (`docker compose` not `docker-compose`)
- 8 vCPU / 32 GB RAM recommended for clusters up to 500 nodes; scale up for larger
- >= 500 GB persistent storage for metrics (default 12-month retention) and logs
- Network access to: cluster head/BCM node, UFM server, BMC management network, IB switch management ports, Slurm controller, shared filesystem

### Compute nodes
- No container runtime required
- systemd
- NVIDIA driver with `nv-hostengine` running (for DCGM)
- Staging area accessible to all nodes (NFS share, cluster filesystem, or SSH copy)

### Optional integrations
| Integration | Requires |
|---|---|
| UFM fabric monitoring | NVIDIA UFM >= 6.x, REST + Telemetry CSV port (:9001) accessible |
| BCM inventory | SSH key auth from obs host to BCM head node |
| BMC/Redfish | Network reach from obs host to all BMC addresses; local account credentials |
| NetQ | NVIDIA NetQ API endpoint + auth token |
| NVLMapper | NVLMapper vendor binary (not included); rack BMC SSH access |
| NCCL Inspector | Set `NCCL_INSPECTOR_DUMP_THREAD_INTERVAL_MICROSECONDS` to a positive value in your job; the shared FS path is polled by the exporter |

---

## Quick Start

```bash
# 1. Clone the repo to your observability host
git clone <your-gitlab-url>/nvis-observability-ncp.git
cd nvis-observability-ncp

# 2. Run the interview installer (run as root or a user in the docker group)
./installer/install.sh

# The installer will:
#   - Ask for your cluster name, Grafana admin password
#   - Walk through each optional integration (BMC, BCM, UFM, NetQ, Slurm, NVLMapper, node agents)
#   - Write .env files from the committed *.example templates (chmod 600, never committed)
#   - Render prometheus.yml from the template (envsubst)
#   - docker compose up -d each enabled stack

# 3. Open Grafana
#   http://<obs-host>:3000   (admin / password you entered)
#   Navigate to: NVIS folder -> nvis-home (landing page)
```

For node agents (push metrics from compute nodes):

```bash
# On obs host: stage binaries to the cluster shared filesystem
cd node-agents/
./stage-binaries.sh          # downloads & stages node_exporter, alloy, dcgm-exporter, cgroup_exporter
./rollout.sh nodes.txt       # SSH rollout to all nodes listed in nodes.txt (one hostname/IP per line)
```

For the head-node relay (required when compute nodes cannot reach the obs host):

```bash
cd node-agents/head-node/
./setup-relay.sh             # installs nginx stream relay on head/BCM node
```

---

## Directory Layout

```
nvis-observability-ncp/
├── installer/
│   ├── install.sh                  # interactive interview installer (start here)
│   └── check-prereqs.sh            # pre-flight dependency check
│
├── platform/                       # central observability stack
│   ├── docker-compose.yml          # core: prometheus, victoriametrics, loki, grafana, alloy
│   ├── .env.example                # template for platform/.env (rendered by installer)
│   ├── alloy/config.alloy          # Alloy pipeline: ingest gateway + self-scrape
│   ├── prometheus/
│   │   ├── prometheus.yml.tmpl     # rendered by installer (cluster label, exporter host)
│   │   └── prometheus.yml          # rendered output (gitignored)
│   ├── loki/loki-config.yaml       # Loki storage config
│   ├── grafana/
│   │   ├── dashboards/             # 21 dashboard JSON files (auto-provisioned)
│   │   └── provisioning/           # datasource and dashboard provider configs
│   └── certs/                      # TLS certs if HTTPS enabled (gitignored contents)
│
├── exporters/                      # site pollers — small stdlib-only Python scripts
│   ├── compose.bmc-bcm-netq.yml    # compose fragment: redfish-bmc, bcm, netq
│   ├── compose.job-monitor.yaml    # compose fragment: slurm-job, llmb-job
│   ├── .env.bmc-bcm-netq.example   # credential templates
│   ├── .env.job-monitor.example
│   ├── prometheus/
│   │   ├── prometheus-fabric.yml   # fabric scrape config (merged by installer)
│   │   └── job-monitor.scrape.yml  # job monitor scrape config
│   ├── bcm/                        # BCM SSH poller
│   ├── cable-audit/                # IB cable audit scanner (cron-triggered)
│   ├── ib-cable-issue/             # cable-issue Prometheus exporter
│   ├── ib-identity/                # node/SU/rack identity join exporter
│   ├── ib-lldp/                    # LLDP neighbor + transceiver exporter
│   ├── ib-switch-audit/            # switch audit exporter
│   ├── llmb-job/                   # training job log parser + exporter
│   ├── netq/                       # NVIDIA NetQ exporter
│   ├── redfish-bmc/                # Redfish BMC exporter
│   ├── slurm-job/                  # Slurm squeue/sacct exporter
│   ├── ufm/                        # UFM REST exporter
│   └── ufm-telemetry/              # UFM Telemetry CSV exporter (PHY/BER/FEC)
│
├── docker-compose.fabric.yml       # compose fragment: ufm, ufm-telemetry, ib-lldp,
│                                   #   ib-cable-issue, ib-switch-audit, ib-identity
│
├── node-agents/                    # compute node agent staging and rollout
│   ├── stage-binaries.sh           # download & stage agent binaries to shared FS
│   ├── rollout.sh                  # SSH rollout: runs install-node.sh on each node
│   ├── install-node.sh             # idempotent per-node installer (systemd units)
│   ├── uninstall-node.sh           # clean removal
│   ├── nodes.txt.example           # node list format
│   ├── head-node/                  # nginx L4 relay for isolated VLANs
│   │   ├── nginx-stream-relay.conf.template
│   │   └── setup-relay.sh
│   ├── systemd/                    # unit files for all four agents + timers
│   └── textfile-collectors/        # NVLink + HCA verbs shell collectors (timer-driven)
│
├── nvlmapper/                      # NVLink down-lane mapper (optional, NVL rack only)
│   ├── nvlmapper_exporter.py       # Prometheus exporter (:9120)
│   ├── parse_nvlmapper.py          # midfile parser
│   ├── run_nvlmapper.sh            # wrapper to invoke vendor binary per-rack
│   ├── config.yml.template         # rack/BMC inventory template (rendered by installer)
│   └── test-samples/               # synthetic fixture data
│
├── terraform/                      # Terraform alternative to file-based provisioning
│   ├── main.tf                     # datasources + dashboards via grafana provider
│   ├── variables.tf
│   ├── versions.tf
│   └── terraform.tfvars.example
│
└── docs/
    ├── ARCHITECTURE.md             # detailed component + data-flow documentation
    └── DASHBOARD_INVENTORY.md      # per-dashboard query and metric inventory
```

---

## Component Reference

### 1. Central Platform

**`platform/docker-compose.yml`** — five containers, one `docker compose up -d`:

| Service | Image | Port | Role |
|---|---|---|---|
| `victoriametrics` | `victoriametrics/victoria-metrics:v1.103.0` | 8428 | Long-term TSDB — system of record. Retention set via `DATA_RETENTION_MONTHS` (default: `12`). |
| `prometheus` | `prom/prometheus:v2.54.1` | 9090 | Scrapes all site exporters + receives remote_write from compute nodes (via Alloy). 15-day local buffer, remote_writes everything to VictoriaMetrics. |
| `loki` | `grafana/loki:3.1.0` | 3100 | Log aggregation: node journals, Slurm logs, job stdout, exporter events. |
| `grafana` | `grafana/grafana:11.1.4` | 3000 | Dashboards. VictoriaMetrics is the default datasource; Prometheus for recent window; Loki for logs. |
| `alloy` | `grafana/alloy:v1.5.1` | 12345 / 9099 / 9098 | Ingest gateway: node agents push metrics to `:9099` and logs to `:9098`. Also self-scrapes the central stack. |

**Datasource UIDs** — all 21 dashboards reference these fixed UIDs. Never rename them:
- `nvis-victoriametrics`
- `nvis-prometheus`
- `nvis-loki`

---

### 2. Node Agents

Four native systemd services per compute node — no container runtime required on the GPU nodes:

| Agent | Port | Purpose |
|---|---|---|
| `dcgm-exporter` | 9400 | Per-GPU metrics via `nv-hostengine`: utilization, power, temperature, XID errors, NVLink counters |
| `node_exporter` | 9100 | Host metrics (CPU, memory, network, disk) + textfile directory (`.prom` files from collectors) |
| `cgroup_exporter` | 9306 | Per-Slurm-job cpu/memory/IO metrics keyed by `jobid` label |
| `Grafana Alloy` | 12345 | Scrapes the three agents above, tails journal and log files, stamps `cluster`/`dgx_flavor`/`node` labels, forwards upstream |

Two textfile collectors run on 30-second systemd timers and write `.prom` files for node_exporter to expose:

- **`host_nvlink_collector.sh`** — per-GPU-link NVLink error counters (GPU-side complement to switch-side UFM telemetry)
- **`hca_verbs_collector.sh`** — IB HCA verbs counters: the "slow retransmission but no switch error" signal that catches GPU-side IB issues before they appear as fabric alarms

**Head-node relay** (`node-agents/head-node/`) — compute nodes typically cannot reach the observability host. The relay is a pass-through nginx `stream` (L4) proxy deployed on the head/BCM node:
- Node Alloy `remote_write` -> `RELAY_HOST:19090` -> obs-host Alloy `:9099`
- Node Alloy Loki push -> `RELAY_HOST:13100` -> obs-host Alloy `:9098`

Alloy's remote_write uses a WAL, so relay or obs-host restarts do not lose data. Sites where compute nodes can reach the obs host directly set `RELAY_HOST` to the obs-host address and skip the relay setup.

**Rollout is idempotent and reversible** — `install-node.sh` backs up existing configs before writing; `uninstall-node.sh` removes units and optionally the binaries.

---

### 3. Site Exporters

Twelve central pollers run as compose services on the observability host. All are small, stdlib-only Python scripts run from stock `python:3.11-slim` with the script bind-mounted — no custom images to build or audit. Credentials arrive as environment variables from gitignored `.env` files.

**Fabric stack** (`docker-compose.fabric.yml`):

| Exporter | Port | Talks to | Key metrics |
|---|---|---|---|
| `ufm` | 9103 | UFM REST API | `ufm_port_*`, `ufm_link_*`, `ufm_alarm_*`, `ufm_switch_*` — IB fabric topology, link state, alarms |
| `ufm-telemetry` | 9105 | UFM Telemetry CSV (:9001) | Per-port PHY counters, BER, effective BER, FEC histograms, symbol errors — the high-frequency fabric health signal |
| `ib-lldp` | 9114 | UFM REST + switch SSH | LLDP neighbor truth table, transceiver detail, SM redundancy check |
| `ib-cable-issue` | 9116 | cable-audit CSV output | `ib_cable_issue_*` — miscabling and cabling anomaly metrics; optional Loki event lines per issue |
| `ib-switch-audit` | 9117 | audit CSVs | Switch hostname, SSH reachability, DNS resolution consistency |
| `ib-identity` | 9118 | topology + placement map | `ib_identity_*` — node to SU/rack/unit join labels; used by every fabric dashboard for drill-down |

**BMC / BCM / network stack** (`exporters/compose.bmc-bcm-netq.yml`):

| Exporter | Port | Talks to | Key metrics |
|---|---|---|---|
| `redfish-bmc` | 9104 | All node BMCs (Redfish) | Temperatures, fan speeds, power draw, PSU status, System Event Log (SEL) entries |
| `bcm` | 9401 | BCM head node (SSH key) | Cluster inventory, node up/down status, health, placement, OS version |
| `netq` | 9109 | NVIDIA NetQ REST API | Ethernet/Cumulus fabric switch and link state (optional) |

**Job monitoring stack** (`exporters/compose.job-monitor.yaml`):

| Exporter | Port | Talks to | Key metrics |
|---|---|---|---|
| `slurm-job` | 9402 | `squeue`/`sacct` on Slurm controller | `slurm_job_info{}`, `slurm_job_node{}` — the JobID-to-node bridge that makes per-job GPU/fabric attribution possible |
| `llmb-job` | 9119 | Training job stdout on shared FS | `llmb_job_*` — per-job step time, TFLOPs, training progress %, NaN/inf detection, stall detection |

**Cable audit scanner** (`exporters/cable-audit/`): runs as a cron job (not a long-lived exporter), compares expected cable topology against live UFM or per-switch SSH discovery, and writes CSV output consumed by `ib-cable-issue` and `ib-switch-audit`. Re-run safe and idempotent.

---

### 4. NVLMapper

For NVL-rack systems (racks with NVSwitch trays): maps which NVLink lanes are currently down, per rack/tray/lane, using the vendor NVLMapper binary that logs into rack BMCs.

- The vendor binary (not included in this repo) writes `midfile__acp_down_connections.txt` files per rack.
- `parse_nvlmapper.py` reads the mid-files; `nvlmapper_exporter.py` exposes `nvl_lane_down_total{rack,tray,lane}` on port **:9120**.
- The exporter monitors file modification times — a file that stops updating is flagged, distinguishing "no lanes down" from "collection stopped."
- Disabled by default (`NVLMAPPER_ENABLED=false`). Set to `true` in the env file (rendered by installer) to activate.
- Rack/BMC inventory lives in gitignored `nvlmapper/config.yml` (rendered from `config.yml.template`).

---

### 5. Dashboards

21 curated dashboards auto-provision into the **NVIS** Grafana folder on container boot (file provider, 30-second reload interval):

**Job-centric (differentiators):**

| Dashboard | What it shows |
|---|---|
| `nvis-job-health` | L1 wallboard — every active Slurm job, GPU power/util, IB health, XID events. The single screen for NOC/operator watch. |
| `nvis-llmb-jobs` | Training job step progress, TFLOPs, stall/NaN flags, per-job timeline |
| `nvis-job-fabric-corr` | JobID to nodes to SUs to fabric health correlation. Drill from a slow job to its GPU and IB lanes. |
| `nvis-job-nccl-corr` | NCCL Inspector correlation: per-collective latency vs. fabric error rate |
| `nvis-job-logs` | Loki log viewer scoped to a job — stdout, Slurm, XID events, NVLink errors, journal |
| `nvis-xid-errors` | XID error timeline and breakdown per GPU, per node, per job |

**Fabric:**

| Dashboard | What it shows |
|---|---|
| `nvis-net-home` | IB fabric landing: switch/port health summary, BER heatmap, alarm count |
| `nvis-ib-overview` | Per-switch port utilization, link state, effective BER, FEC histogram |
| `nvis-ib-cable-audit` | Cable audit results: mismatches, unresolved neighbors, audit run history |
| `nvis-ib-offenders` | Top-N offending ports/switches by error rate, with drill-to-switch links |

**Fleet / hardware:**

| Dashboard | What it shows |
|---|---|
| `cluster-overview` | Cluster-wide GPU utilization, power, node count, active jobs |
| `gpu-health` | Per-GPU temperature, power, clock, throttle, XID history |
| `node-deep-dive` | Single-node detail: all GPUs, NVLink, IB, CPU, memory, disk |
| `bcm-cluster` | BCM cluster inventory, node states, health over time |
| `nvis-hwval-cluster-health` | Hardware validation cluster health view |
| `nvis-hwval-gpu-profiling` | HW validation GPU profiling metrics |
| `nvis-hwval-sensors-temps` | Sensor and thermal view for validation runs |

**Reporting:**

| Dashboard | What it shows |
|---|---|
| `executive` | Cluster utilization %, GPU-hours consumed, availability SLA trend |
| `business-insights` | Job throughput, queue depth, resource efficiency |
| `impact-analysis` | Incident impact: GPU-hours lost, jobs affected, MTTR |
| `nvis-home` | Landing page with navigation tiles and cluster status summary |

All dashboards use `$datasource` (VictoriaMetrics/Prometheus) and `$cluster` template variables — no hardcoded site values. See `docs/DASHBOARD_INVENTORY.md` for a full per-dashboard metric and query inventory.

---

### 6. Terraform (alternative provisioning)

`terraform/` provides an alternative to the file-based Grafana provisioner for teams that manage Grafana state through IaC. The `grafana/grafana` Terraform provider is used to create datasources and upload every dashboard JSON.

```bash
cd terraform/
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: grafana_url, grafana_auth, cluster_name
terraform init
terraform apply
```

`overwrite = true` makes it idempotent — safe to re-apply after adding dashboards. State and `terraform.tfvars` are gitignored. The file-based provisioner in `platform/docker-compose.yml` works out of the box without Terraform; this is purely optional.

---

## Installer Deep-Dive

`installer/install.sh` is the single entry point. Run it on the observability host from the repo root:

```bash
./installer/install.sh
```

It runs in interview mode, section by section:

1. **Pre-flight** — Docker, Compose v2, network reach checks via `installer/check-prereqs.sh`
2. **Core** — cluster name (used as Prometheus `cluster` external label and Grafana `$cluster` default), Grafana admin password, obs-host address, data retention period
3. **Node agents** — whether to enable, path to staging area (NFS/shared FS), agent binary versions
4. **Head-node relay** — whether the compute nodes need a relay; if yes, relay host address
5. **BMC / Redfish** — whether to enable, BMC target CSV path, Redfish credentials
6. **BCM** — whether to enable, BCM address, SSH key path
7. **UFM / IB fabric** — whether to enable UFM REST, UFM Telemetry, IB fabric exporters; UFM host, credentials, telemetry port
8. **NetQ** — whether to enable, API endpoint, auth token
9. **Slurm job monitor** — whether to enable, Slurm controller hostname, SSH key or local
10. **LLM benchmark job log scanning** — whether to enable, shared FS log path pattern
11. **NVLMapper** — whether to enable, rack BMC targets, config rendering

For each section:
- Credentials are collected with `read -s` (no echo) and confirmed; they are held in shell variables and written only to chmod-600 `.env` files — never logged or printed.
- Existing `.env` files are backed up with a timestamp before overwriting.
- `prometheus.yml` is rendered from `prometheus.yml.tmpl` via `envsubst`.
- `docker compose up -d` is called for each enabled stack at the end.

Re-running the installer is safe. It backs up and re-renders `.env` files, then restarts affected services.

---

## Port Map

| Port | Service | Where |
|---|---|---|
| 3000 | Grafana UI | Obs host |
| 3100 | Loki | Obs host (internal) |
| 8428 | VictoriaMetrics | Obs host (internal) |
| 9090 | Prometheus | Obs host (internal) |
| 9098 | Alloy Loki ingest | Obs host |
| 9099 | Alloy metric ingest | Obs host |
| 9100 | node_exporter | Each compute node |
| 9103 | ufm exporter | Obs host |
| 9104 | redfish-bmc exporter | Obs host |
| 9105 | ufm-telemetry exporter | Obs host |
| 9109 | netq exporter | Obs host |
| 9114 | ib-lldp exporter | Obs host |
| 9116 | ib-cable-issue exporter | Obs host |
| 9117 | ib-switch-audit exporter | Obs host |
| 9118 | ib-identity exporter | Obs host |
| 9119 | llmb-job exporter | Obs host |
| 9120 | nvlmapper exporter | Obs host |
| 9306 | cgroup_exporter | Each compute node |
| 9400 | dcgm-exporter | Each compute node |
| 9401 | bcm exporter | Obs host |
| 9402 | slurm-job exporter | Obs host |
| 12345 | Alloy UI / self-scrape | Obs host (internal) |
| 13100 | Head-node relay -> Loki | Head node |
| 19090 | Head-node relay -> Alloy | Head node |

All obs-host exporter ports use `network_mode: host`; Prometheus reaches them at `host.docker.internal:<port>` via the docker host-gateway alias.

---

## Secrets & Security Posture

- **No secrets in git.** All credentials exist only in gitignored, chmod-600 `.env` files written by the installer and held only in memory during the interview. `.gitignore` files in `platform/`, `exporters/`, and `nvlmapper/` enforce this.
- **No custom images.** All exporters run from stock `python:3.11-slim` with read-only bind-mounted scripts. You can audit the full supply chain with no custom layer.
- **BCM auth is SSH-key only.** Password auth is not supported for BCM.
- **UFM, BMC, switch, and NetQ passwords** are environment-injected at container start — never baked into images or compose files.
- **Sample/fixture data** in `exporters/*/data/` and `nvlmapper/test-samples/` is fully synthetic: RFC 5737 documentation-range IPs (`198.51.100.x`), placeholder hostnames (`leaf01`, `gpu000001`), and fabricated MAC/GUIDs. It exists only to exercise parsers in demo environments.
- **TLS** — Grafana can be placed behind a TLS-terminating reverse proxy. Certificate files in `platform/certs/` are gitignored; mount them into the Grafana container via the compose file.
- **Firewall** — the port map above is the full external surface. Restrict access to Grafana (:3000) to your cluster management network; all other ports can be firewall-closed to external access.

---

## Upgrade & Re-run

**To update dashboards only:**
```bash
git pull
# Grafana file provider reloads every 30s — no restart needed
```

**To update exporter scripts:**
```bash
git pull
docker compose -f docker-compose.fabric.yml restart ufm ufm-telemetry  # or whichever changed
```

**To update platform images (Prometheus, Loki, Grafana, etc.):**
```bash
git pull
docker compose -f platform/docker-compose.yml pull
docker compose -f platform/docker-compose.yml up -d
```

**To re-run the installer** (e.g., to add a new integration or change credentials):
```bash
./installer/install.sh
# Existing .env files are backed up before overwriting
```

**To update node agents** after a binary change:
```bash
cd node-agents/
./stage-binaries.sh       # re-stage updated binaries to shared FS
./rollout.sh nodes.txt    # re-runs install-node.sh on each node (idempotent)
```

---

## Troubleshooting

**Grafana shows "No data" on all panels:**
- Check that `platform/.env` was rendered by the installer (not a blank copy).
- `docker compose -f platform/docker-compose.yml logs prometheus` — look for scrape errors.
- Verify `host.docker.internal` resolves: `docker compose exec prometheus ping -c1 host.docker.internal`.

**Exporter shows no metrics:**
- `docker compose -f docker-compose.fabric.yml logs ufm` — check for auth/connection errors.
- Credentials are in the gitignored `.env` — confirm the file exists and has the correct values.
- The installer writes `chmod 600 .env`; confirm the compose service user can read it.

**Node metrics missing:**
- Confirm `nv-hostengine` is running on the compute node (`systemctl status nv-hostengine`).
- Check Alloy is running: `systemctl status alloy`.
- Verify relay ports: `ss -tlnp | grep -E '19090|13100'` on the head node.

**Dashboards show "datasource not found":**
- The fixed datasource UIDs (`nvis-victoriametrics`, `nvis-prometheus`, `nvis-loki`) must match. If you imported dashboards manually, check `platform/grafana/provisioning/datasources/`.
- Restarting Grafana re-provisions from the file provider: `docker compose restart grafana`.

**UFM Telemetry exporter returns empty metrics:**
- UFM Telemetry CSV streaming must be enabled in UFM (port 9001 by default). Confirm with `curl http://<UFM_HOST>:9001/csv`.

**Cable audit shows stale data:**
- Check the cable-audit cron ran and review the cron log.
- `NEIGHBOR_CMD` in the cable-audit config may need IB discovery supplement if the switch list is incomplete.

---

## Contributing

This repo is structured for portability — every value is an env var, every secret is gitignored, no custom images. Contributions that preserve these properties are welcome.

- Open issues for bugs or missing integrations.
- PRs: run `installer/check-prereqs.sh` on a clean host before submitting; include a synthetic-data fixture if adding a new exporter.
- Do not commit real IP addresses, hostnames, passwords, tokens, certificates, or cluster-specific configuration — the pre-commit gitignore patterns will catch most cases, but double-check with `git diff --staged` before pushing.
- Datasource UIDs in dashboards must remain `nvis-victoriametrics`, `nvis-prometheus`, `nvis-loki` — changing them breaks all 21 dashboards.

---

*NVIS Observability Appliance is a generalized, deploy-anywhere platform. No site-specific data is included in this repository.*
