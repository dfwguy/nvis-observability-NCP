# Job Monitor — exporter component

Answers one operator question for a DGX SuperPOD cluster: **"where did my validation /
benchmark job land, and is it actually making progress?"** Two small, stdlib-only
Prometheus exporters, each a `python:3.11-slim` docker-compose service with the script
bind-mounted (no custom image), mirroring the rest of the stack.

| Exporter | Port | Source | What it gives you |
|----------|------|--------|-------------------|
| `llmb-job/llmb_job_exporter.py`  | 9119 | the harness's per-job **training stdout logs** on the shared FS | `llmb_job_step`, `llmb_job_total_steps`, `llmb_job_step_seconds`, `llmb_job_tflops_per_gpu`, `llmb_job_nan_iterations`, `llmb_job_log_age_seconds` (stall detector), `llmb_job_info`, `llmb_job_count` |
| `slurm-job/slurm_job_exporter.py` | 9402 | `squeue` + `sacct` | `slurm_job_info`, **`slurm_job_node`** (jobid→node bridge), per-user/partition counts, per-job elapsed/CPU/RSS/ncpus |

The two compose to one picture: `slurm_job_node` joins a job to the per-node GPU / fabric
metrics (DCGM, IB), while the LLMB metrics tell you whether that job is training, stalled,
or going NaN — the log is the source of truth even when the Slurm accounting row is empty.

## Deploy

```bash
# 1. Fill site values (the interview installer does this for you):
cp exporters/.env.job-monitor.example .env      # then edit; .env is gitignored

# 2. Bring the two exporters up alongside the core stack:
docker compose -f docker-compose.yml -f exporters/compose.job-monitor.yaml up -d

# 3. Add the scrape jobs (exporters/prometheus/job-monitor.scrape.yml) under
#    scrape_configs: in prometheus.yml and reload Prometheus.
```

## No leaked data

Every site value is an `.env` variable (`SLURM_JOB_LOG_DIR`, `SLURM_LOG_GLOB`,
`SLURM_REGISTRY_SQLITE`, `SLURM_CONF`, `BCM_HOST`, `CLUSTER_NAME`/`DGX_FLAVOR`, …).
No IPs, hostnames, credentials, or cluster code-names live in these files; secrets stay
in `.env` / in-memory and are never committed. See each sub-directory's `README.md`.
