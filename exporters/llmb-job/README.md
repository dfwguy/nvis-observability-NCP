# llmb-job-exporter (:9119) — the per-Slurm-job validation/benchmark scraper

The **crown jewel** of the Job Monitor component. Every job the LLMB / dgxc-benchmarking
harness runs writes exactly ONE training stdout log to the shared filesystem:

```
${SLURM_JOB_LOG_DIR}/workloads/*/experiments/*/*/*/log-*_<JOBID>_0.out
```

This exporter globs those logs, **tail-reads** each one (progress lines are at the end),
and emits per-JOBID Prometheus metrics. No NCCL plugin, no dump-interval, no per-collective
JSON — just the job's own log, which always exists and always has the truth. Filesystem-only:
no SSH, no credentials.

## Two lines it scrapes (per the head-node spec)

```
Step Time : 15.29s GPU utilization: 588.4MODEL_TFLOP/s/GPU   -> step_seconds, tflops_per_gpu
[ts] iteration  50/  50 | ... nan iterations: 0 |             -> step, total_steps, nan_iterations
```

Identity labels (`workload/model/dtype/scale`) come from an optional harness sqlite
registry, falling back to the experiment-name directory when the row is empty. **Progress
and perf always come from the log**, never the registry. Pass/fail authority stays with
Slurm (see `slurm_job_info` / `sacct` in the sibling exporter).

## Stall detection

`llmb_job_log_age_seconds` = now − log mtime. A job that Slurm reports RUNNING but whose
log age exceeds a threshold is **STALLED** — dashboards alert on exactly this.

## Metrics

`llmb_job_step`, `llmb_job_total_steps`, `llmb_job_step_seconds`, `llmb_job_tflops_per_gpu`,
`llmb_job_nan_iterations`, `llmb_job_log_age_seconds`, `llmb_job_info`, `llmb_job_count`,
plus exporter self-health (`llmb_exporter_up`, `llmb_last_scan_timestamp_seconds`,
`llmb_scan_duration_seconds`). Health endpoint: `GET /healthz`.

Bounded + cached: only the last `LLMB_EXPORTER_TAIL_BYTES` (24 KiB) of each log is read,
and a finished log (unchanged mtime) is parsed once and cached — scanning hundreds of
historical jobs every 30 s stays cheap.

## Configuration (all env-driven — nothing site-specific is baked in)

| Env var | Default | Meaning |
|---------|---------|---------|
| `SLURM_JOB_LOG_DIR` | `/data/llmb` | root where validation-job logs + results land (was `LLMB_ROOT`, still accepted) |
| `SLURM_LOG_GLOB` | `workloads/*/experiments/*/*/*/log-*_*_0.out` | log glob; relative to the root unless absolute; supports `**` |
| `SLURM_REGISTRY_SQLITE` | `<root>/.llmb/jobs.sqlite3` | optional job registry for identity labels; ignored when absent (a note is logged to stderr) |
| `DGX_FLAVOR` | `cluster` | value of the `cluster` label (falls back to `CLUSTER_NAME`) |
| `LLMB_EXPORTER_PORT` | `9119` | listen port |
| `LLMB_EXPORTER_INTERVAL` | `30` | scan interval (s) |
| `LLMB_EXPORTER_HEALTHZ_STALE` | `150` | `/healthz` fails if last scan older than this (s) |
| `LLMB_EXPORTER_TAIL_BYTES` | `24576` | bytes tail-read per log |

## One-shot self-test (no server)

```bash
python3 llmb_job_exporter.py /path/to/log/root   # prints the metrics text once and exits
```
