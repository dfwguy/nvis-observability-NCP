# ufm-telemetry exporter (:9105)

Polls the **open** UFM Telemetry CSV endpoint
(`http://<ufm>:9001/csv/metrics`, no auth), resolves CSV columns by name, and
emits per-switch-port PHY / BER / FEC / congestion counters plus switch-level
temperature / power / voltage. Pure stdlib (urllib + csv) — no pip deps.

## Metrics

Per active switch port: `SymbolErrorCounterExtended`, `PortXmitWaitExtended`,
`PortXmitDiscardsExtended`, `PortRcvErrorsExtended`, `PortXmitDataExtended`,
`PortRcvDataExtended` (and the other `*Extended` counters),
`ufm_port_xmit_cong_time`, `phy_hist_bin{bin}` (sparse BER histogram),
`phy_raw_errors_lane{0..3}`, `phy_fec_corrected_blocks_total`,
`phy_fec_uncorrectable_blocks_total`, `eff_ber`. Per switch:
`ufm_switch_temperature_celsius`, `ufm_switch_power_watts`,
`ufm_switch_power_psu_watts{psu}`, `ufm_switch_voltage_volts`. Plus
`ufm_telemetry_up` / `ufm_telemetry_ports_emitted` /
`ufm_telemetry_scrape_duration_seconds`.

## Environment

`UFM_HOST` (+ `UFM_TELEMETRY_PORT`, default 9001) builds the default CSV URL;
override with `UFM_CSV_URL` / `UFM_TELEMETRY_URL`. `CLUSTER_NAME`,
`POLL_INTERVAL` (90), `FETCH_TIMEOUT` (60), `SU_HOST_FILTER` (optional
comma-separated host_name prefixes to scope cardinality), `PORT` (9105).

## Quick check

```bash
curl -s http://$UFM_HOST:9001/csv/metrics | head -1   # should be a CSV header row
```
