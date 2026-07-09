# NVIS Dashboard Inventory

Curated, generalized Grafana dashboards shipped by the **dashboards** component.

- **Location (repo):** `platform/grafana/dashboards/*.json`
- **How they load:** the platform `docker-compose.yml` bind-mounts `./grafana/dashboards`
  into the Grafana container at `/etc/grafana/dashboards`; the file provider
  (`platform/grafana/provisioning/dashboards/dashboards.yaml`) loads every `*.json`
  into the **NVIS** folder on boot and every 30 s.
- **Datasources (provisioned, fixed UIDs):**
  - `nvis-victoriametrics` — Prometheus-API datasource, **default** (long-range metrics)
  - `nvis-prometheus` — Prometheus (recent/high-res + alert rules)
  - `nvis-loki` — Loki (logs)

  Every dashboard references metrics via the `${datasource}` template variable
  (default `nvis-victoriametrics`) and logs via `${loki_datasource}` / `${loki}`
  (default `nvis-loki`), so they work against either the VictoriaMetrics or
  Prometheus backend with no edits.

## Generalization applied (public-repo hygiene)

All site-specific values were stripped before publishing:

| Class of internal value                              | Generalized to |
|------------------------------------------------------|----------------|
| DGX flavor codename hardcoded in titles/labels       | `DGX SuperPOD` / `cluster` label value |
| `dgx_flavor` template var hardcoded to a flavor       | custom var, default `DGX SuperPOD` (installer sets from `DGX_FLAVOR`) |
| `cluster` template var pinned to a site value         | reset to **All** (`$__all`), repopulated live via `label_values(...)` |
| Site / customer identifiers in titles & descriptions  | removed / replaced with `DGX SuperPOD` / `NVIS` |
| Internal switch / node / host names                   | `leaf-switch`, `node-xxxx`, `observability host` |
| Hardcoded cluster scale figures (node / GPU counts)   | `fleet-scale`, `thousands of GPUs` |
| Internal IPs (RFC-1918: `10.x` / `172.16–31.x` / `192.168.x`) | `${OBS_HOST}` (none remain in the curated set) |

No credentials, tokens, emails, internal IPs, internal hostnames, or customer/site
identifiers remain in any dashboard JSON.

## Common template variables

- `$datasource` (metrics, default `nvis-victoriametrics`), `$loki_datasource` / `$loki` (logs)
- `$cluster` — `label_values(DCGM_FI_DEV_GPU_TEMP, cluster)`, defaults to All
- `$node`, `$gpu`, `$jobid` / `$JobID` — scoped `label_values(...)` drill-downs
- `$dgx_flavor` — display-only label for the DGX platform (default `DGX SuperPOD`)

---

## 1. Slurm-job-centric dashboards (the differentiators)

These are driven by the NVIS custom exporters (`llmb-job`, `slurm-job`, NCCL Inspector
artifacts, cgroup/DCGM correlation) and are the reason to run NVIS on a training cluster.

### 🏃 Benchmark Jobs — Progress & Health (LLMB) — `nvis-llmb-jobs`
Per-Slurm-job LLM-benchmark (LLMB) progress, throughput and stall/NaN detection from the
`llmb_job_exporter`. Answers "is my benchmark job advancing, stalled, or diverging?"
Datasources: VictoriaMetrics.
Key queries:
- `count(llmb_job_info{jobid=~"$JobID"})` — jobs in selection
- `count((llmb_job_log_age_seconds{jobid=~"$JobID"} > 300) unless (llmb_job_step == llmb_job_total_steps))` — **stalled** jobs (log >5 m, not done)
- `count(llmb_job_nan_iterations{jobid=~"$JobID"} > 0)` — NaN/divergence detected
- `llmb_job_step{jobid=~"$JobID"}` vs `llmb_job_total_steps` — progress bar
- `max(llmb_job_step_seconds{jobid=~"$JobID"} and llmb_job_log_age_seconds < 120)` — slowest live step (s)

### 🚦 Job Health — Where Did It Break (L1) — `nvis-job-health`
L1 triage: one screen showing WHERE a Slurm job broke — outcome banner, per-domain health
tiles (GPU compute / GPU errors / network-fabric / storage), and the fabric-miscabling
"money shot" (GPUs healthy, leaves miswired). Datasources: VictoriaMetrics + Loki.
Key queries:
- `count(count by(node)(cgroup_info{jobid=~"$jobid"}))` — nodes the job actually ran on (cgroup→job map)
- `avg(DCGM_FI_DEV_POWER_USAGE * on(instance) group_left cgroup_info{jobid=~"$jobid"})` — GPU compute (avg power) scoped to the job
- `count(max_over_time((bitmap_and(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS,232)!=bool 0)[$__range:1m]) * on(instance) group_left cgroup_info{jobid=~"$jobid"} > 0)` — GPUs throttled during the job
- `count(ib_cable_issue{issue=~"Wrong-neighbor|Extra-cable"} * on(node) group_left() ...)` — miscabled fabric on the job's leaves
- LogQL: `sum(count_over_time({filename=~"/var/log/(kern.log|syslog)", node=~"$node"} |~ "(?i)xid" ... [$__range]))` — XID incidents on the job's nodes

### 📜 Job Logs (L2) — XID · Slurm · Errors — `nvis-job-logs`
Log-centric L2 view: deduped XID incidents, Slurm lifecycle events, and error lines for the
selected job/nodes, with benign XID codes (13/31/32/43/45) excluded. Datasources: Loki.
Key queries (LogQL):
- `sum(count_over_time({filename=~"/var/log/(kern.log|syslog)", node=~"$node"} |~ "NVRM: Xid" !~ "Xid [(][^)]*[)]: (13|31|32|43|45)[,:]" [$__range]))` — total non-benign XID events
- `count(sum by (node, gpu, pci, xid) (count_over_time({... node=~"$node"} |~ "(?i)xid" ...)))` — distinct (deduped) XID incidents
- `count(sum by (xid) (count_over_time({...} |~ "(?i)xid" ...)))` — distinct XID codes seen
- `{filename=~"/var/log/(kern.log|syslog)", node=~"$node"} |~ "(?i)xid" ... |~ "$log_search"` — raw XID stream, newest first

### XID Errors — DGX SuperPOD — `nvis-xid-errors`
Fleet-wide XID error dashboard sourced entirely from logs (no DCGM XID metric on the cluster):
occurrences by XID code & node, plus raw per-event timeline. Datasources: Loki.
Key queries (LogQL):
- `sum(count_over_time({filename=~"/var/log/(kern.log|syslog)", node=~"$node"} |~ "NVRM: Xid" !~ "Xid [(][^)]*[)]: (13|31|32|43|45)[,:]" [$__range]))` — total XID events (benign excluded)
- `count(sum by (node) (count_over_time({...} |~ "NVRM: Xid" ...)))` — nodes affected
- `sum by (node, xid) (count_over_time({...} |~ "(?i)xid" | regexp "(?i)xid *(?:[(]pci:...)" [$__range]))` — occurrences by XID code & node
- `{filename=~"/var/log/(kern.log|syslog)", node=~"$node"} |~ "(?i)xid"` — raw per-occurrence timeline

### 🧬 Job → NCCL / NVLink Correlation (L2) — `nvis-job-nccl-corr`
Given a `$JobID`, correlates all NCCL-Inspector instrumentation signals (NCCL Inspector,
comm config, topo fingerprint, nvlmapper down-lanes, host NVLink, nsys CE/SM, DCGM PROF,
HCA/SHARP, step-time jitter) onto one screen, with per-signal freshness gates.
Datasources: VictoriaMetrics + Loki.
Key queries:
- `max(stddev_over_time(exemplar_train_step_seconds{jobid=~"$JobID"}[$__range]))` — step-time σ (jitter)
- `max(quantile_over_time(0.99, exemplar_train_step_seconds{jobid=~"$JobID"}[$__range]))` — step p99
- `(max(time()-timestamp(nccl_inspector_coll_count{node=~"$job_nodes"})) < bool 90)` — S1 NCCL-Inspector freshness gate
- `(max(time()-timestamp(nccl_topo_fingerprint{node=~"$job_nodes"})) < bool 90)` — S3 topo-fingerprint present
- LogQL: `{job="nccl-debug"} |~ "$job_nodes"` — NCCL debug log for the job's hosts

### Job ⇄ Fabric Correlation — `nvis-job-fabric-corr`
Given a Slurm JobID, overlays its GPU-power trace against the IB-fabric health of the exact
SU fabric / rails that job occupied — answers "was this slow run the fabric's fault?"
Datasources: VictoriaMetrics.
Key queries:
- `slurm_job_info{jobid=~"$JobID"}` — job identity/state
- `count(count by(su)(ib_node_placement{node=~"$job_nodes"}))` — SU(s) the job occupied
- `count(ib_switch_info{su=~"$job_sus"})` — switches on the job's SU-fabric path
- `avg(DCGM_FI_DEV_POWER_USAGE{node=~"$job_nodes"})` — GPU power on job nodes (vs fabric disturbance)
- `max(slurm_job_elapsed_seconds{jobid=~"$JobID"})` — elapsed

---

## 2. Platform observability

### NVIS Home — `nvis-demo-home` (file `nvis-home.json`)
Landing page / KPI beacon and navigation hub for the whole NVIS suite. Datasources: VictoriaMetrics + Loki.
Key queries:
- `count(count by(node)(DCGM_FI_DEV_GPU_TEMP{cluster=~"$cluster"}))` — nodes online
- `count(DCGM_FI_DEV_GPU_TEMP{cluster=~"$cluster"})` — total GPUs
- `sum(DCGM_FI_DEV_POWER_USAGE{cluster=~"$cluster"})` — total GPU power
- `count(count by(jobid)(cgroup_info{cgroup=~".*/slurmstepd.scope/job_[0-9]+$"}))` — active Slurm jobs
- LogQL: `sum(count_over_time({filename=~"/var/log/(kern.log|syslog)"} |~ ` + "`NVRM: Xid`" + ` [1h]))` — XID events (1 h)

### Cluster Overview — `cluster-overview`
Fleet node/GPU health at a glance: reporting vs down, utilization, ECC, throttle, temps.
Datasources: VictoriaMetrics + Loki.
Key queries:
- `count(up{job="node-exporter", cluster=~"$cluster", node=~"$node"} == 0)` — nodes down
- `count(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{cluster=~"$cluster"} > 0)` — GPUs with double-bit ECC
- `count(bitmap_and(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{cluster=~"$cluster"}, 232) > 0)` — GPUs HW/thermal throttled
- `avg(DCGM_FI_DEV_GPU_UTIL{cluster=~"$cluster", node=~"$node"})` / `max(DCGM_FI_DEV_GPU_TEMP{...})` — util / max temp
- LogQL: `{filename=~"/var/log/(kern.log|syslog)"} |~ "(?i)xid|nvlink"` — XID/NVLink events

### GPU Health — ECC, Throttle, NVLink — `gpu-health`
Issue-first GPU health: ECC (SBE/DBE), remapped rows + row-remap failure, decoded throttle
reasons, PCIe replays, NVLink, temps — offender tables not aggregate gauges.
Datasources: VictoriaMetrics + Loki.
Key queries:
- `count(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{...} > 0)` — GPUs with double-bit ECC (critical/replace)
- `count(DCGM_FI_DEV_ROW_REMAP_FAILURE{...} > 0)` — GPUs with row-remap FAILURE
- `count(bitmap_and(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{...}, 232) > 0)` — GPUs throttling (HW/thermal/power)
- `max by (node, gpu, modelName, pci_bus_id) (DCGM_FI_DEV_ECC_DBE_VOL_TOTAL{...})` — ECC offender table
- LogQL: `sum by (node) (count_over_time({filename=~"/var/log/(kern.log|syslog)"} |~ "(?i)\bxid\b" [$__range]))` — XID offenders by node

### Node Deep Dive — Hardware, OS, Storage, Network — `nvis-node-deep-dive`
Per-node hardware/OS/storage/network deep dive with fleet roll-ups: disk pressure, load,
NIC/IB error ports, EDAC memory ECC, OOM kills. Datasources: VictoriaMetrics + Loki.
Key queries:
- `count(count by(node)((1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|..."} / node_filesystem_size_bytes{...}) > 0.85))` — nodes with disk > 85 %
- `sum by(node,device)(increase(node_infiniband_port_errors_received_total[1h]) + increase(node_infiniband_symbol_error_total[1h]))` — IB error ports
- `(node_edac_uncorrectable_errors_total>0) or (node_edac_correctable_errors_total>0)` — memory ECC nodes
- `increase(node_vmstat_oom_kill[1h]) > 0` — nodes with OOM kills (1 h)
- LogQL: `{filename=~"/var/log/(kern.log|syslog)", node=~"$node"} |= "kernel" |~ "(?i)(error|gpu|nvrm|xid|nvlink|oom|ecc)"` — kernel/GPU errors

### BCM Cluster — `bcm-cluster-v1` (file `bcm-cluster.json`)
Base Command Manager (BCM/BrightCluster) inventory & health from the `bcm-exporter`:
node up/down, health-check failures, provisioning/boot syslog. Datasources: VictoriaMetrics + Loki.
Key queries:
- `sum(bcm_node_up{cluster=~"$cluster", job="bcm-exporter"})` / `sum(1 - bcm_node_up{...})` — nodes up / down
- `count(bcm_node_health_check{cluster=~"$cluster", job="bcm-exporter"} == 0)` — failing health checks
- `bcm_node_up{...} == 0` — down-node offender table
- `bcm_scrape_duration_seconds{cluster=~"$cluster"}` — exporter scrape health
- LogQL: `{cluster=~"$cluster", job="syslog"} |~ "(?i)error|fail|down|provision|boot|health"` — head-node provisioning log

### Executive Overview — Cluster ROI, Allocation & Health — `nvis-executive`
Exec-level roll-up: GPU allocation/utilization, availability, power/thermal, GPU-hours,
offender drill-downs. Datasources: VictoriaMetrics + Loki.
Key queries:
- `100 * sum(DCGM_FI_DEV_FB_USED{...}) / sum(DCGM_FI_DEV_FB_USED{...} + DCGM_FI_DEV_FB_FREE{...})` — GPU memory allocated %
- `count(DCGM_FI_DEV_GPU_UTIL{cluster=~"$cluster"} > 5)` — GPUs actively working (>5 % util)
- `avg_over_time(count(DCGM_FI_DEV_GPU_UTIL{...})[24h:5m]) * 24` — GPU-hours powered (24 h)
- `sum(DCGM_FI_DEV_POWER_USAGE{cluster=~"$cluster"})` — total GPU power draw
- LogQL: `sum by (node) (count_over_time({cluster=~"$cluster"} |~ "(?i)xid" [$__range]))` — XID events by node

### Operational Intelligence — Allocation, Usage & Performance — `nvis-business-insights` (file `business-insights.json`)
Business/operational view: active nodes, GPU allocation, team usage, token analytics,
training performance, job activity. Datasources: VictoriaMetrics + Loki.
Key queries:
- `count(up{job="node-exporter",cluster=~"$cluster"} == 1)` / `== 0` — active / offline nodes
- `avg(DCGM_FI_DEV_GPU_UTIL{cluster=~"$cluster"})` — fleet GPU utilization
- `sum(DCGM_FI_DEV_FB_USED{...})/(sum(DCGM_FI_DEV_FB_USED{...})+sum(DCGM_FI_DEV_FB_FREE{...}))*100` — VRAM allocated %
- `slurm_jobs_running_total{cluster=~"$cluster"}` — active jobs
- LogQL: `{job=~"slurm|slurmd", node=~"$node"} |~ "(?i)(job|step|fail|cancel|complete|launch)"` — Slurm job events

### Cross-Domain Impact Analysis — Job → Network → GPU — `nvis-impact-analysis`
Root-cause / blast-radius correlation across job, network and GPU domains: resource pressure,
OOM, HW errors, throttle, and log evidence in one place. Datasources: VictoriaMetrics + Loki.
Key queries:
- `sum(increase(node_vmstat_oom_kill{cluster=~"$cluster",node=~"$node"}[$__range]))` — OOM kills (range)
- `count((DCGM_FI_DEV_ECC_DBE_VOL_TOTAL>0) or (DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS>0) or ...)` — GPUs with HW errors
- `count(bitmap_and(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS{...},232) > 0)` — GPUs throttling
- `topk(25, node_load1{cluster=~"$cluster",node=~"$node"})` — most-loaded nodes
- LogQL: `sum by(node) (count_over_time({... job="syslog"} |~ "(?i)xid|nvlink" [$__range]))` — XID/NVLink events by node

---

## 3. Network / InfiniBand fabric

Backed by the UFM-telemetry exporter (`ufm_*`), IB host/switch exporters (`ib_*`,
`node_infiniband_*`) and the cable-audit exporter (`ib_cable_*`).

### NVIS Network — IB Observability Home — `nvis-net-home`
Landing / KPI beacon + navigation hub for the InfiniBand observability suite.
Datasources: VictoriaMetrics + Loki.
Key queries:
- `ufm_fabric_healthy{cluster=~"$cluster"}` — fabric healthy flag
- `count(ufm_switch_up{cluster=~"$cluster"}==0)` — switches down
- `100*count(node_infiniband_state_id{cluster=~"$cluster"}==4)/clamp_min(count(node_infiniband_state_id{...}),1)` — links active %
- `sum(increase(node_infiniband_link_downed_total{cluster=~"$cluster"}[1h]))` — link flaps (1 h)
- `sum(ib_cable_issue{cluster=~"$cluster"})` — open cable issues

### InfiniBand — Fabric Overview & Health Scorecard — `nvis-ib-overview`
Top scorecard for the IB fabric: switch/link/port counts, alarms, topology distribution and
trends. Datasources: VictoriaMetrics + Loki.
Key queries:
- `max(ufm_up)` — UFM exporter up
- `sum(ufm_switch_count{cluster=~"$cluster"})` / `sum(ufm_link_count{...})` — switches / links
- `sum(ufm_port_active_count{cluster=~"$cluster"})` — ports active
- `sum(ufm_alarms_total{cluster=~"$cluster"})` — active alarms
- `ibl_fabric_health_score{cluster=~"$cluster"}` — composite fabric health score

### InfiniBand — Bad / Slow / Degraded Connections — `nvis-ib-offenders`
Daily-triage offenders: slow speed (< NDR/XDR), degraded width (< 4x), down ports, and a live
composite worst-connection score (host + UFM views). Datasources: VictoriaMetrics + Loki.
Key queries:
- `count(ufm_port_up{cluster=~"$cluster",speed=~"SDR|DDR|QDR|FDR|EDR|HDR"} == 1)` — slow links (below NDR/XDR)
- `count(ufm_port_up{cluster=~"$cluster",width=~"1x|2x|3x"} == 1)` — degraded-width links (< 4x)
- `count(ufm_port_up{cluster=~"$cluster",speed!="Unknown"} == 0)` — down ports
- `count(ib_link_health_score{cluster=~"$cluster"} > 0.5)` — critical host-links
- `topk(40, (ib_link_health_score{...} > 0) * on(node,device) group_left(leaf,leaf_port,su,rack) ib_host_rail_leaf)` — worst host-connections

### InfiniBand — Cabling Audit (live) — `nvis-ib-cable-audit`
Run-over-run cabling audit: compares the newest cable-issue scan against the previous scan to
show NEW vs FIXED miscablings and scan freshness. Datasources: VictoriaMetrics.
Key queries:
- `ib_cable_audit_age_seconds` / `ib_cable_audit_timestamp_seconds * 1000` — audit age / last-scan time
- `ib_cable_audit_compare_available` — is a prior scan available to diff
- `sum(ib_cable_issue_new_total) and on() (ib_cable_audit_compare_available == 1)` — NEW issues since last audit
- `sum(ib_cable_issue_resolved_total) and on() (ib_cable_audit_compare_available == 1)` — RESOLVED since last audit
- `sum(ib_cable_issue_new_total) - sum(ib_cable_issue_resolved_total)` — net change

---

## 4. Hardware validation

Deployment / burn-in dashboards keyed on `node_hwmon_*`, `node_edac_*`, DCGM temps/power/clocks.

### DGX SuperPOD Cluster Health — `nvis-hwval-cluster-health`
Issue-first fleet hardware health: only nodes/sensors with a fault appear (unreachable,
over-temp, hwmon alarm, EDAC uncorrectable, etc.). Datasources: VictoriaMetrics.
Key queries:
- `count(up{job="node-exporter", node=~"$node"} == 0)` — nodes down
- `count(count by(node)((node_hwmon_temp_celsius{node=~"$node"} < 150) >= node_hwmon_temp_crit_celsius{node=~"$node"} * 0.9))` — nodes with a hot sensor (≥ 90 % of crit)
- `count(count by(node)(node_edac_uncorrectable_errors_total{node=~"$node"} > 0))` — nodes with EDAC uncorrectable errors
- `max(node_hwmon_temp_celsius{node=~"$node"} < 150)` — max board sensor temp
- `max(DCGM_FI_DEV_GPU_TEMP{node=~"$node"} < 150)` — max GPU temp

### DGX SuperPOD - GPU Profiling — `nvis-hwval-gpu-profiling`
Per-component GPU temp/power/clock profiling with topk outlier views instead of all-fleet
line charts. Datasources: VictoriaMetrics + Loki.
Key queries:
- `topk(20, DCGM_FI_DEV_GPU_TEMP{cluster=~"$cluster", node=~"$node"} < 150)` — hottest GPU chips
- `topk(20, DCGM_FI_DEV_MEMORY_TEMP{cluster=~"$cluster", node=~"$node"} < 150)` — hottest HBM modules
- `topk(20, DCGM_FI_DEV_POWER_USAGE{cluster=~"$cluster", node=~"$node"})` — highest-power GPUs
- `sum(DCGM_FI_DEV_POWER_USAGE{cluster=~"$cluster", node=~"$node"})` — total fleet GPU power
- LogQL: `{filename=~"/var/log/(kern.log|syslog)"} |~ "(?i)(xid|nvlink)" |~ "(?i)${search:raw}"` — XID/NVLink log events

### DGX SuperPOD - Per-Sensor Temperatures — `nvis-hwval-sensors-temps`
Granular per-component temperatures (Redfish `/Sensors`, 80 s scrape): topk outliers + hot-sensor
counts + thermal-violation gates. Datasources: VictoriaMetrics + Loki.
Key queries:
- `max(node_hwmon_temp_celsius{cluster=~"$cluster", node=~"$node"} < 150)` — max board sensor temp
- `count((node_hwmon_temp_celsius{...} < 150) > $hot)` — sensors over the `$hot` threshold
- `count(DCGM_FI_DEV_THERMAL_VIOLATION{cluster=~"$cluster", node=~"$node"} > 0)` — GPUs in thermal violation
- `topk(20, DCGM_FI_DEV_GPU_TEMP{...} < 150)` — hottest GPU cores (outliers)
- LogQL: `{cluster=~"$cluster"} |~ "(?i)overt|over.?temp|thermal.?trip|thermal.?slowdown|ierr"` — over-temp / thermal-trip SEL events

---

## Prerequisites for these dashboards to render data

1. **Grafana up** — `http://<host>:3000/api/health` returns `{"database":"ok"}`.
2. **Datasources provisioned** — `nvis-victoriametrics` and `nvis-loki` exist (they are
   file-provisioned by the platform component; UIDs are referenced verbatim in every panel).
3. **Metrics flowing** — DCGM (`DCGM_FI_DEV_*`), node-exporter (`node_*`), and the NVIS
   exporters (`bcm_*`, `slurm_job_*`, `llmb_job_*`, `ib_*`, `ufm_*`, `cgroup_info`, `nccl_*`)
   are being scraped/remote-written. Empty panels usually mean the corresponding exporter is
   not yet deployed, not a broken dashboard.
4. **Logs flowing** — Loki is receiving `/var/log/{kern.log,syslog}` and `job="syslog"` /
   `job="nccl-debug"` streams for the XID/log-based panels.
