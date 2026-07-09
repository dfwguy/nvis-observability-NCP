# ib-lldp exporter (:9114)

Augments the UFM exporter with two data planes:

1. **UFM REST** (every `UFM_POLL`, default 60s): SM master/standby redundancy
   (`ibl_sm_*`), pkeys (`ibl_pkey_*`), fabric switch/HCA counts, and a composite
   fabric health score (`ibl_fabric_health_*`). Pure stdlib.
2. **Switch LLDP sweep** (every `SWEEP_INTERVAL`, default 30m): SSHes the
   switches in concurrency-capped batches (with an inter-batch pause so switch
   mgmt CPUs are never hit all at once) to collect transceiver presence + SSH
   reachability (`ibl_switch_ssh_reachable`, `ibl_transceiver_modules`). Uses
   `paramiko` (pip-installed at container start).

## Switch list

By default the switch IPs to sweep are auto-discovered from UFM
(`/resources/systems`). Set `SWITCH_LIST` to `name=ip,name=ip,...` (or bare
IPs) to override for air-gapped / no-UFM sites. Set `SWEEP_ENABLED=0` to run
the UFM plane only (no switch SSH).

## Environment

`UFM_HOST/USER/PASSWORD`, `CLUSTER_NAME`, `SWITCH_SSH_USER`,
`SWITCH_SSH_PASSWORD` (`NVOS_USERNAME`/`NVOS_PASSWORD` also accepted),
`SWITCH_LIST`, `SWEEP_ENABLED` (1), `SWEEP_INTERVAL` (1800),
`SWEEP_BATCH_SIZE` (15), `SWEEP_BATCH_PAUSE` (5), `SSH_TIMEOUT` (15),
`PORT` (9114).

The exporter always serves `ibl_up 1` once started, even if the UFM poll or the
sweep is still pending / unavailable.
