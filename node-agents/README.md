# NVIS Node Agents

Per-node observability agents for GPU compute nodes in a DGX SuperPOD-class
cluster, plus the head-node relay that carries their telemetry to the ObsVM.
Everything here is **native systemd** on the nodes (no container runtime
required) and driven entirely by environment variables — no site IPs, hostnames,
or credentials are baked in.

## What lands on each node

| Agent            | Port  | Purpose                                                            |
|------------------|-------|-------------------------------------------------------------------|
| `dcgm-exporter`  | 9400  | Per-GPU metrics via the local `nv-hostengine` (`localhost:5555`)  |
| `node_exporter`  | 9100  | Host CPU/mem/disk/net **+ textfile collectors** (`.prom` files)   |
| `cgroup_exporter`| 9306  | Per-Slurm-job cpu/mem/io from cgroup v2, keyed by `jobid`         |
| `alloy`          | 12345 | Scrapes 9400/9100/9306/self, tails logs+journal, pushes upstream  |

Textfile collectors (run on a systemd timer, write into the node_exporter
textfile dir so they need **zero** extra exporter):

- `host_nvlink_collector.sh` → `su1_nvlink.prom` — GPU-side NVLink per-link
  CRC/replay/recovery errors, link up/speed, fabric-manager/IMEX health, GPU
  fabric registration state.
- `hca_verbs_collector.sh` → `ib_hca.prom` — IB HCA verbs/port counters
  (`packet_seq_err`, `local_ack_timeout_err`, `port_xmit_wait`, …): the
  "slow-but-no-switch-error" retransmission signal invisible to switch counters.

## Data path: node → relay → ObsVM

```
  ┌────────────── GPU compute node (native systemd) ──────────────┐
  │  dcgm-exporter:9400  node_exporter:9100  cgroup_exporter:9306  │
  │        └──────────────┬───────────────────────┘               │
  │                   Grafana Alloy :12345                         │
  │        (scrape → relabel add cluster/dgx_flavor/instance/node) │
  └───────────────┬───────────────────────────┬───────────────────┘
       remote_write │                    loki push │
                    ▼                              ▼
      RELAY_HOST:RELAY_RW_PORT        RELAY_HOST:RELAY_LOKI_PORT
                (19090)                        (13100)
  ┌───────────────┴───────────────────────────┴───────────────────┐
  │   Head node — nginx L4 stream relay (head-node/, compose/)     │
  │   19090 ─► METRICS_TARGET:9090     13100 ─► METRICS_TARGET:3100 │
  └───────────────┬───────────────────────────┬───────────────────┘
                  ▼                            ▼
        ObsVM Prometheus/VM :9090        ObsVM Loki :3100
```

**Why the relay?** Compute nodes commonly sit on an isolated compute/management
VLAN and cannot reach the ObsVM directly — only the head/BCM node can. The head
node runs a pass-through nginx `stream` proxy; node Alloy agents target
`RELAY_HOST` and the relay forwards raw TCP to the ObsVM. If your nodes *can*
reach the ObsVM directly, set `RELAY_HOST=METRICS_TARGET` with
`RELAY_RW_PORT=9090` / `RELAY_LOKI_PORT=3100` and skip the relay.

Alloy `remote_write` is WAL-buffered, so a relay or ObsVM restart does not drop
node metrics — they backfill when the path recovers.

## Deploy

```bash
# 0) fill in env (see .env.node-agents.example) — the interview installer does this
export METRICS_TARGET=<obsvm>  RELAY_HOST=<head>  CLUSTER_NAME=<name>

# 1) head node: bring up the relay (skip if nodes reach the ObsVM directly)
METRICS_TARGET=$METRICS_TARGET ./head-node/setup-relay.sh --docker

# 2) admin/head host with the share mounted: stage binaries (arch auto-detected)
SHARE=/cm/shared/nvis/bin ./stage-binaries.sh

# 3) ObsVM/admin host: roll out to the fleet (canary first, then batches)
NODE_LIST=./nodes.txt ./rollout.sh          # or NODE_DISCOVERY_CMD='sinfo -h -N -o %N'
```

`install-node.sh` is idempotent (re-run safe; `FORCE=1` to re-render) and
reversible (`uninstall-node.sh`, `PURGE=1` also clears the `.prom` outputs). Each
node prints a machine-readable `NVIS_RESULT … verdict=PASS|FAIL` line that
`rollout.sh` aggregates into `rollout-results.tsv`.

## Prerequisites

- **SSH to the nodes** from wherever you run `rollout.sh` (directly or via
  `SSH_JUMP`), key-based, as `SSH_USER`.
- **DCGM present** on each node: a running `nv-hostengine` on `localhost:5555`
  (`nvidia-dcgm.service`) and `libdcgm.so.4` on the library path.
- **Relay reachable** from the nodes: `RELAY_HOST:RELAY_RW_PORT` and
  `:RELAY_LOKI_PORT` must accept TCP.
- **Shared staging dir** (`SHARE`) mounted on every node and populated by
  `stage-binaries.sh`.

## Files

| Path                                   | What                                              |
|----------------------------------------|---------------------------------------------------|
| `install-node.sh`                      | Runs ON a node; installs/renders all agents       |
| `uninstall-node.sh`                    | Reverses `install-node.sh`                         |
| `rollout.sh`                           | Drives the fleet over SSH (canary + batches)      |
| `stage-binaries.sh`                    | Populates `SHARE` (arm64/amd64 auto-detect)       |
| `dcgm/dcgm-counters.csv`               | DCGM field set → `/etc/dcgm-exporter/counters.csv`|
| `alloy/config.alloy.template`          | Reference Alloy config (installer renders it)     |
| `systemd/*.service`, `*.timer`         | Reference units for the agents + collector timers |
| `textfile-collectors/*.sh`             | NVLink + IB-HCA `.prom` collectors                |
| `head-node/nginx-stream-relay.conf.template` | nginx `stream` relay config template        |
| `head-node/setup-relay.sh`             | Render + deploy the relay (docker or host nginx)  |
| `compose/head-relay.compose.yml`       | Relay as a compose service                         |
| `.env.node-agents.example`             | Env fragment (placeholders only)                  |
| `nodes.txt.example`                    | Node inventory example                            |
