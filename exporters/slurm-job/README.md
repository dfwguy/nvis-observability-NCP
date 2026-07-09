# slurm-job-exporter (:9402) — generic Slurm job & node bridge

Turns `squeue` (live) + `sacct` (accounting) into Prometheus metrics, with the key
**`slurm_job_node`** edge that lets a dashboard join a running job to the per-node GPU /
fabric metrics (DCGM, InfiniBand) it is actually using. Stdlib only; works with any
username source Slurm sees (local, LDAP, SAML, AD).

## Two ways to run

- **local** (recommended when you can): the exporter runs directly on the Slurm
  controller / login node via the provided `slurm-job-exporter.service` systemd unit,
  where `squeue`/`sacct`/munge already work. Set `SLURM_MODE=local`.
- **ssh**: the containerized compose service SSHes to the controller and runs the
  commands there (reuses the head-node SSH key). Set `BCM_HOST` (and `SLURM_MODE` empty
  auto-selects ssh).

`SLURM_BIN` / `SLURM_CONF` are applied on whichever side runs the commands — handy when
`squeue`/`sacct` are not on the default PATH and there is no DNS SRV for the config.

## Metrics

Counts: `slurm_jobs_running_total`, `slurm_jobs_pending_total`, `slurm_jobs_active_total`,
`slurm_users_active_total`, `slurm_users_running_total`, `slurm_jobs_active_by_user`,
`slurm_jobs_active_by_partition`, plus the `sacct` window aggregates
(`slurm_jobs_recent_*`, `slurm_users_recent_total`).

Per job: `slurm_job_info` (jobid → user/state/partition/nnodes/ncpus label carrier),
`slurm_job_elapsed_seconds`, `slurm_job_cpu_seconds`, `slurm_job_maxrss_bytes`,
`slurm_job_averss_bytes`, `slurm_job_ncpus`, and **`slurm_job_node`** — value `1` for each
`(jobid,node)` pair, with the Slurm compressed hostlist (`gpu[001-004,009],login01`)
expanded to individual node names.

Health: `slurm_exporter_last_success_timestamp`, `up`, and `GET /health`.

## Configuration (all env-driven)

| Env var | Default | Meaning |
|---------|---------|---------|
| `SLURM_MODE` | auto (`ssh` if `BCM_HOST` else `local`) | where the commands run |
| `BCM_HOST` / `BCM_USER` / `BCM_JUMP` / `BCM_SSH_KEY` / `BCM_SSH_PORT` | — / `root` / — / `~/.ssh/id_ed25519` / `22` | SSH target for ssh mode |
| `SLURM_BIN` | (PATH) | dir holding `squeue`/`sacct` |
| `SLURM_CONF` | — | `slurm.conf` path (exported before the commands run) |
| `CLUSTER_NAME` / `DGX_FLAVOR` | `cluster` | `cluster` label value |
| `SLURM_PORT` | `9402` | listen port |
| `SCRAPE_INTERVAL` | `30` | collect interval (s) |
| `SACCT_HOURS` | `24` | rolling accounting window |

No IPs, hostnames, or credentials are committed — every site value is an `.env` variable
or the systemd `EnvironmentFile`, and the SSH key is bind-mounted read-only, never copied
into the repo.
