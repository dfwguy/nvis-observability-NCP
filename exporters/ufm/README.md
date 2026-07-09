# ufm exporter (:9103)

Polls the NVIDIA UFM Enterprise REST API (`/ufmRest/`) and exposes InfiniBand
fabric metrics in Prometheus text format. Pure stdlib (urllib) — no pip deps.

## Metrics (prefix `ufm_`)

Fabric version/health, per-switch state/severity/firmware/temperature/uptime,
per-port state/tier/speed/BER + xmit/rcv/errors/discards counters, link
active/total counts, active alarms by severity/type/description, recent events,
and (optional) Cluster Minder node boot/firmware health.

## Environment

`UFM_HOST` (required), `UFM_USER` (default `ufmsystem`), `UFM_PASSWORD`
(`UFM_PASS` also accepted), `UFM_CLUSTER_MINDER` (default false), `CLUSTER_NAME`,
`VERIFY_SSL` (default false), `PORT` (9103), `SCRAPE_INTERVAL` (30),
`UFM_TIMEOUT` (15).

## Quick check

```bash
curl -sk -u "$UFM_USER:$UFM_PASSWORD" https://$UFM_HOST/ufmRest/app/ufm_version
```

When UFM is unreachable the exporter still serves `ufm_up 0` (never crashes).
