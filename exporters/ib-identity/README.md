# ib-identity exporter (:9118)

Topology/identity **join bridge**. Emits `value=1` info-metrics that carry the
full canonical label set, so every raw per-port / per-link series (`ufm_*`,
`node_infiniband_*`, `ib_cable_*`, ...) can be label-joined back to the expected
topology, and blast-radius / Cytoscape topology consumers can resolve any
switch / link / host identity without fuzzy matching.

- `generate_identity.py` — builds the metrics text + `alias.json` +
  `blast-index.json` from the ground-truth artifacts (import-safe `build_all()`).
- `ib_identity_exporter.py` — stdlib HTTP server; caches the build and serves
  `/metrics`, `/alias.json`, `/blast-index.json`, `/healthz`.

## Metric families

`ib_switch_info`, `ib_link_peer`, `ib_host_rail_leaf`, `ib_leaf_spine`,
`ib_leaf_leaf`, `ib_node_placement`, plus `ib_identity_up` /
`ib_identity_series_total` / `ib_identity_build_timestamp_seconds` meta.

## Ground-truth artifacts (`data/`)

This repo ships **tiny sanitized SAMPLE files** so the exporter runs out of the
box. Replace them with your site's real artifacts (mount a host directory over
`/app/data`, or edit these files) to emit real identity.

| File | Schema |
|---|---|
| `expected-topology.json` | `{"meta":{...}, "nodes":[{id,role,rack,unit,su,model,...}], "edges":[{id,a_node,a_port,z_node,z_port,cls,a_su,z_su,a_optic,z_optic}]}`. `role` ∈ host/leaf/spine/ufm; `cls` ∈ host-leaf/leaf-spine/leaf-leaf/leaf-ufm. |
| `placement_map.json` | `{"<host>": ["<su>","<rack>","U<unit>","<node_in_rack>"], ...}` — the authoritative host inventory (canonical hostnames). |
| `ufm_switches_new.csv` | `ip,name,system_name,role,state,model,fw_version,sw_version,guid` — UFM REST switch inventory (see cable-audit README for how to export it). |

`expected-topology.json` is derived from your cabling BOM/workbook. Host-side
edge endpoints that carry a workbook server-profile name
(`SU<x>-R<rack>-Server-<n>`) are rewritten to the canonical hostname via
`placement_map.json`; set `HOST_NAME_PREFIX` (default `gpu`) to your host
hostname prefix.

## Environment

`PORT` (9118), `CLUSTER_NAME`, `FABRIC`, `HOST_NAME_PREFIX`, `IB_IDENTITY_DATA`
(default `/app/data`), `REFRESH_SECONDS` (3600).

## Regenerate the files on disk

```bash
IB_IDENTITY_DATA=./data IB_IDENTITY_OUT=/tmp python3 generate_identity.py
# writes /tmp/metrics.prom, /tmp/alias.json, /tmp/blast-index.json
```
