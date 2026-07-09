# ib-cable-issue exporter (:9116)

Serves the cable-audit scanner's `cable-issue_*.csv` output (see
`../cable-audit`) as `ib_cable_*` Prometheus metrics. Reads the newest snapshot
in `CABLE_WATCH_DIR` every `REFRESH_INTERVAL` seconds. Runtime dep:
`prometheus-client` (see `requirements.txt`).

## Metrics

`ib_cable_issue{node,rack,unit,issue,source_port,expected_neighbor,discovered_neighbor,su,fabric}`,
`ib_cable_switch_unreachable`, `ib_cable_issue_total{issue}`,
`ib_cable_issue_by_switch{switch,su,fabric}`, `ib_cable_repatch_worklist[_total]`,
`ib_cable_audited_switches_total`, `ib_cable_active_issues_total`,
`ib_cable_snapshot_timestamp`, `ib_cable_scrape_success`,
`ib_cable_last_refresh_timestamp`.

Issue categories: `Media Unplugged`, `Extra-cable`, `Wrong-neighbor`,
`Wrong-port` (real miswiring), plus `Unreachable-device` (coverage gap, emitted
as `ib_cable_switch_unreachable`, not a miswiring).

## Environment

`CABLE_PORT` (9116), `CABLE_WATCH_DIR` (`/data`), `CABLE_ISSUE_CSV` /
`CABLE_TARGET_CSV` (explicit path overrides), `SWITCH_MAP_JSON`
(`/data/switch_su_map.json`, for `su` enrichment), `FABRIC`, `REFRESH_INTERVAL`
(60), `LOKI_URL` (optional; best-effort per-issue log lines).
