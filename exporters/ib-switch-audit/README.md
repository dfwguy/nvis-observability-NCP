# ib-switch-audit exporter (:9117)

Reads two ground-truth CSVs and exposes an InfiniBand switch **identity +
firmware/software audit** view. Owns the audit/firmware fields under
`ib_switch_audit_info` (the ib-identity exporter owns `ib_switch_info` /
topology fields) so the two never collide.

## Metrics

`ib_switch_audit_info`, `ib_switch_hostname_mismatch[_total]`,
`ib_switch_by_role`, `ib_switch_fw_drift`, `ib_switch_sw_drift`,
`ib_switch_swversion_coverage`, `ib_switch_ssh_reachable_total`,
`ib_switch_total`, plus `ib_switch_audit_age_seconds` /
`ib_switch_audit_reload_total` / `ib_switch_audit_exporter_up` freshness.

## Ground-truth artifacts (`data/`)

Sanitized SAMPLE files ship here; replace with your site's real ones (or mount a
host dir over `/data` refreshed by your SSH-audit cron).

| File | Schema |
|---|---|
| `ufm_switches_new.csv` | `ip,name,system_name,role,state,model,fw_version,sw_version,guid` — UFM REST inventory (switch set of record). See the cable-audit README for an export snippet. |
| `nvos_all_switches_audit_*.csv` | the SSH/DNS/UFM cross-audit; newest by `audit_timestamp_utc` wins. Columns: `ip,audit_timestamp_utc,ufm_guid,ufm_role,ufm_state,ufm_system_name,expected_hostname,reported_hostname,hostname_status,ssh_status,resolution_status,ufm_vs_switch,ufm_description`. `hostname_status` ∈ MATCH/MISMATCH; `ssh_status` ∈ CONNECTED/…. |

The two are joined by `ip`, with `guid` carried as the stable cross-source
identity. The exporter re-reads them every `REFRESH_INTERVAL` seconds.

## Environment

`PORT`/`LISTEN_PORT` (9117), `LISTEN_ADDR`, `CLUSTER_NAME`, `UFM_CSV`
(default `/data/ufm_switches_new.csv`), `AUDIT_GLOB`
(default `/data/nvos_all_switches_audit_*.csv`), `REFRESH_INTERVAL` (60).
