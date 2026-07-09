#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# stage-binaries.sh — populate the cluster-shared dir ($SHARE) that install-node.sh
# copies from. Run ONCE from a host that can reach the internet (or a mirror) and
# can write to the share (e.g. the head node / an admin box with the NFS mounted).
#
# Detects CPU arch automatically (arm64 for Grace / amd64 for x86) so the same
# share serves both. dcgm-exporter has no standalone release tarball — it ships
# inside NVIDIA's container image, so it is extracted from there if `docker`/
# `nerdctl` is available, otherwise you must drop the binary in by hand.
#
# Env overrides:
#   SHARE                 target dir (default /cm/shared/nvis/bin)
#   NODE_EXPORTER_VER     default 1.8.2
#   ALLOY_VER             default 1.5.1
#   CGROUP_EXPORTER_VER   default 0.10.3   (treydock/cgroup_exporter)
#   DCGM_EXPORTER_IMAGE   default nvcr.io/nvidia/k8s/dcgm-exporter:3.3.9-3.6.1-ubuntu22.04
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SHARE="${SHARE:-/cm/shared/nvis/bin}"
NODE_EXPORTER_VER="${NODE_EXPORTER_VER:-1.8.2}"
ALLOY_VER="${ALLOY_VER:-1.5.1}"
CGROUP_EXPORTER_VER="${CGROUP_EXPORTER_VER:-0.10.3}"
DCGM_EXPORTER_IMAGE="${DCGM_EXPORTER_IMAGE:-nvcr.io/nvidia/k8s/dcgm-exporter:3.3.9-3.6.1-ubuntu22.04}"

case "$(uname -m)" in
  aarch64|arm64) GOARCH=arm64 ;;
  x86_64|amd64)  GOARCH=amd64 ;;
  *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
esac
echo ">> staging into ${SHARE} for GOARCH=${GOARCH}"
mkdir -p "${SHARE}"
tmp="$(mktemp -d)"; trap 'rm -rf "${tmp}"' EXIT

fetch(){ curl -fSL --retry 3 "$1" -o "$2"; }

# ── node_exporter ────────────────────────────────────────────────────────────
echo ">> node_exporter ${NODE_EXPORTER_VER}"
fetch "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VER}/node_exporter-${NODE_EXPORTER_VER}.linux-${GOARCH}.tar.gz" "${tmp}/ne.tgz"
tar -xzf "${tmp}/ne.tgz" -C "${tmp}"
install -m0755 "${tmp}/node_exporter-${NODE_EXPORTER_VER}.linux-${GOARCH}/node_exporter" "${SHARE}/node_exporter"

# ── grafana alloy ────────────────────────────────────────────────────────────
echo ">> alloy ${ALLOY_VER}"
fetch "https://github.com/grafana/alloy/releases/download/v${ALLOY_VER}/alloy-linux-${GOARCH}.zip" "${tmp}/alloy.zip"
( cd "${tmp}" && unzip -oq alloy.zip )
install -m0755 "${tmp}/alloy-linux-${GOARCH}" "${SHARE}/alloy"

# ── cgroup_exporter (treydock) ───────────────────────────────────────────────
echo ">> cgroup_exporter ${CGROUP_EXPORTER_VER}"
fetch "https://github.com/treydock/cgroup_exporter/releases/download/v${CGROUP_EXPORTER_VER}/cgroup_exporter-${CGROUP_EXPORTER_VER}.linux-${GOARCH}.tar.gz" "${tmp}/cge.tgz"
tar -xzf "${tmp}/cge.tgz" -C "${tmp}"
install -m0755 "${tmp}/cgroup_exporter-${CGROUP_EXPORTER_VER}.linux-${GOARCH}/cgroup_exporter" "${SHARE}/cgroup_exporter"

# ── dcgm-exporter (extract from the NVIDIA container image) ───────────────────
if [[ -f "${SHARE}/dcgm-exporter" ]]; then
  echo ">> dcgm-exporter already staged — leaving as-is"
else
  RUNTIME=""
  command -v docker  >/dev/null 2>&1 && RUNTIME=docker
  command -v nerdctl >/dev/null 2>&1 && RUNTIME="${RUNTIME:-nerdctl}"
  if [[ -n "${RUNTIME}" ]]; then
    echo ">> dcgm-exporter — extracting from ${DCGM_EXPORTER_IMAGE} via ${RUNTIME}"
    cid="$(${RUNTIME} create "${DCGM_EXPORTER_IMAGE}")"
    ${RUNTIME} cp "${cid}:/usr/bin/dcgm-exporter" "${SHARE}/dcgm-exporter" 2>/dev/null \
      || ${RUNTIME} cp "${cid}:/usr/local/bin/dcgm-exporter" "${SHARE}/dcgm-exporter"
    ${RUNTIME} rm -f "${cid}" >/dev/null
    chmod 0755 "${SHARE}/dcgm-exporter"
  else
    echo "!! dcgm-exporter NOT staged: no docker/nerdctl to extract from ${DCGM_EXPORTER_IMAGE}."
    echo "   Copy the dcgm-exporter binary (matching your GPU driver/DCGM) to ${SHARE}/dcgm-exporter by hand."
  fi
fi

# ── dcgm-counters.csv + install-node.sh + textfile collectors ────────────────
here="$(cd "$(dirname "$0")" && pwd)"
install -m0644 "${here}/dcgm/dcgm-counters.csv"                    "${SHARE}/dcgm-counters.csv"
install -m0755 "${here}/install-node.sh"                          "${SHARE}/install-node.sh"
install -m0755 "${here}/textfile-collectors/host_nvlink_collector.sh" "${SHARE}/host_nvlink_collector.sh"
install -m0755 "${here}/textfile-collectors/hca_verbs_collector.sh"   "${SHARE}/hca_verbs_collector.sh"

echo ">> staged contents of ${SHARE}:"
ls -l "${SHARE}"
echo ">> done. Now run rollout.sh from the ObsVM/admin host."
