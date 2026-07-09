# BCM (Base Command Manager) Exporter

Central poller that **SSHes** to the BCM / Base Command Manager (BrightCluster
Manager) head node, runs `cmsh` plus standard Linux commands, and exports
cluster inventory and per-node status as Prometheus metrics on **:9401**.

Auth is **SSH-key only** — the container mounts `~/.ssh` read-only and never
sees a BCM password. Pure Python standard library (needs the `openssh-client`
package, installed by the compose service at start).

## What it collects

* BCM head: version, hostname, OS/product, CPU, RAM, load, uptime, disk, kernel
* Managed nodes: total / up / down, per-node `bcm_node_up{node,category,status}`,
  GPU-node counts, aggregate CPU/RAM/GPU
* Slurm: active job count (if Slurm present)
* Node-exporter-compatible series (`up`, `node_uname_info`, …) so topology
  discovers the BCM host

Key metrics: `bcm_up`, `bcm_info`, `bcm_managed_nodes_total/up/down`,
`bcm_node_up`, `bcm_nodes_by_status`, `bcm_collection_duration_seconds`,
`bcm_collection_errors_total`.

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `BCM_HOST` | *(req)* | BCM head node IP or hostname. |
| `BCM_USER` | `root` | SSH user. |
| `BCM_JUMP` | — | Jump host `user@host` if BCM is not directly reachable. |
| `BCM_SSH_KEY` | `~/.ssh/id_ed25519` | SSH private key path (mounted read-only). |
| `BCM_SSH_PORT` | `22` | SSH port. |
| `CLUSTER_NAME` | `nvis-cluster` | `cluster` label on every metric. |
| `SCRAPE_INTERVAL` | `60` | Seconds between BCM polls. |
| `PORT` | `9401` | Listen port. |
| `BCM_TIMEOUT` | `30` | SSH command timeout (seconds). |

No credentials are stored on disk — only the SSH key is mounted. Configure
passwordless key-based SSH from the observability host to the BCM head first.

## Run

```bash
# standalone
BCM_HOST=<bcm-ip-or-host> BCM_JUMP=user@jumphost CLUSTER_NAME=my-cluster \
  python3 bcm_exporter.py

# docker-compose (from repo root)
docker compose -f docker-compose.yml -f exporters/compose.bmc-bcm-netq.yml up -d bcm-exporter
curl -s localhost:9401/metrics | grep bcm_
```

## Endpoints

* `/metrics` — Prometheus scrape
* `/health` — liveness
