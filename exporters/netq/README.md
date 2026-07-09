# NetQ Exporter (optional)

Prometheus exporter for the **NVIDIA Cumulus NetQ** REST API. Polls the NetQ
telemetry server every `SCRAPE_INTERVAL` seconds and exposes switch inventory,
events, links and interface counters on **:9109**.

> **Optional integration.** Enable this only when the cluster's Ethernet /
> Cumulus fabric is managed by NetQ. Clusters that use only the InfiniBand /
> UFM path (or have no NetQ) can leave it disabled — nothing else depends on it.

## What it scrapes

| NetQ endpoint | Prometheus metric |
|---------------|-------------------|
| `GET /netq/api/v1/endpoints/devices` | `netq_device_info`, `netq_device_up` |
| `GET /netq/api/v1/endpoints/events` | `netq_event_count{severity,type}` |
| `GET /netq/api/v1/endpoints/links` | `netq_link_up{src,dst,...}` |
| `GET /netq/api/v1/endpoints/interfaces` | `netq_interface_bytes_in/out`, `netq_interface_oper_up` |

Plus self-monitoring: `netq_up`, `netq_scrape_errors_total`,
`netq_scrape_duration_seconds`. Every series carries a `cluster` label.

## Environment variables

| Variable | Default | Notes |
|----------|---------|-------|
| `NETQ_ENDPOINT` | *(req)* | Base URL, e.g. `https://netq.example.com`. (`NETQ_URL` accepted as alias.) |
| `NETQ_TOKEN` | *(opt)* | Bearer token; takes priority over user/pass. **Secret — env only.** |
| `NETQ_USER` | *(opt)* | API username; used with `NETQ_PASS` when no token is set. |
| `NETQ_PASS` | *(opt)* | API password. **Secret — env only.** |
| `CLUSTER_NAME` | `""` | `cluster` label on every metric. |
| `PORT` | `9109` | Listen port for `/metrics` and `/healthz`. |
| `SCRAPE_INTERVAL` | `30` | Seconds between full NetQ polls. |
| `HTTP_TIMEOUT` | `15` | Per-request timeout (seconds). |
| `VERIFY_SSL` | `true` | Verify NetQ TLS certificate. |

You must supply **either** `NETQ_TOKEN` **or** `NETQ_USER` + `NETQ_PASS`.

## Dependencies

Third-party libs (installed by the compose service from `requirements.txt`):
`requests`, `urllib3`, `prometheus_client`.

## Run

```bash
# docker-compose (from repo root)
docker compose -f docker-compose.yml -f exporters/compose.bmc-bcm-netq.yml up -d netq-exporter
curl -s localhost:9109/metrics | grep netq_

# standalone
pip install -r requirements.txt
NETQ_ENDPOINT=https://netq.example.com NETQ_TOKEN='***' CLUSTER_NAME=my-cluster \
  python3 netq_exporter.py
```

## Endpoints

* `/metrics` — Prometheus scrape
* `/healthz` (`/-/healthy`) — liveness (503 if the last poll is stale)
