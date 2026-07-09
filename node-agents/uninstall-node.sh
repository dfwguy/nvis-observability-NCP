#!/usr/bin/env bash
# uninstall-node.sh — reverse install-node.sh on a GPU node. Runs ON the node.
#
#   PURGE=1  also delete the textfile .prom output dir contents.
set -uo pipefail
HOST="$(hostname -s)"
PURGE="${PURGE:-0}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"

# stop + disable collector timers first (they depend on node_exporter)
for t in nvis-nvlink-collector nvis-hca-collector; do
  systemctl disable --now "${t}.timer" 2>/dev/null || true
  rm -f "/etc/systemd/system/${t}.timer" "/etc/systemd/system/${t}.service"
done

for svc in alloy dcgm-exporter cgroup-exporter node_exporter; do
  systemctl disable --now "${svc}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${svc}.service"
done
systemctl daemon-reload

rm -f /usr/bin/alloy \
      /usr/local/bin/dcgm-exporter /usr/local/bin/node_exporter /usr/local/bin/cgroup_exporter \
      /usr/local/bin/host_nvlink_collector.sh /usr/local/bin/hca_verbs_collector.sh
rm -rf /etc/alloy /etc/dcgm-exporter /var/lib/alloy
[[ "${PURGE}" == "1" ]] && rm -f "${TEXTFILE_DIR}"/*.prom 2>/dev/null || true

echo "[${HOST}] NVIS node agents removed"
