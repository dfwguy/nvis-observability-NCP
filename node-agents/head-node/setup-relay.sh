#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-relay.sh — render + deploy the head-node nginx stream relay that carries
# blind compute-node telemetry to the ObsVM. Run ON the head node.
#
# Two deploy modes:
#   --docker   (default) render config and (re)start an nginx:alpine container
#              publishing the relay ports. Requires docker; or use the compose
#              fragment compose/head-relay.compose.yml instead.
#   --system   render into /etc/nginx/nginx.conf and reload the host nginx
#              (package nginx-full / any build with the stream module).
#
# Env (all site values — nothing hardcoded):
#   METRICS_TARGET    ObsVM host/IP running Prometheus(:9090)+Loki(:3100)  [required]
#   RELAY_RW_PORT     listen port forwarded to :9090   (default 19090)
#   RELAY_LOKI_PORT   listen port forwarded to :3100   (default 13100)
#   NGINX_IMAGE       container image for --docker mode (default nginx:alpine)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODE="--docker"
[[ "${1:-}" == "--system" ]] && MODE="--system"
[[ "${1:-}" == "--docker" ]] && MODE="--docker"

METRICS_TARGET="${METRICS_TARGET:?set METRICS_TARGET to the ObsVM host/IP}"
export METRICS_TARGET
export RELAY_RW_PORT="${RELAY_RW_PORT:-19090}"
export RELAY_LOKI_PORT="${RELAY_LOKI_PORT:-13100}"
NGINX_IMAGE="${NGINX_IMAGE:-nginx:alpine}"

here="$(cd "$(dirname "$0")" && pwd)"
tpl="${here}/nginx-stream-relay.conf.template"
[[ -f "${tpl}" ]] || { echo "missing template ${tpl}" >&2; exit 1; }

rendered="$(mktemp)"
# only substitute our three known vars so nothing else in the file is touched
envsubst '${METRICS_TARGET} ${RELAY_RW_PORT} ${RELAY_LOKI_PORT}' < "${tpl}" > "${rendered}"
echo ">> rendered relay config (target=${METRICS_TARGET} rw=${RELAY_RW_PORT} loki=${RELAY_LOKI_PORT})"

if [[ "${MODE}" == "--system" ]]; then
  install -m0644 "${rendered}" /etc/nginx/nginx.conf
  nginx -t
  systemctl reload nginx || systemctl restart nginx
  echo ">> host nginx reloaded"
else
  command -v docker >/dev/null 2>&1 || { echo "docker not found; use --system or the compose fragment" >&2; exit 1; }
  install -d /etc/nvis-relay
  install -m0644 "${rendered}" /etc/nvis-relay/nginx.conf
  docker rm -f nvis-head-relay >/dev/null 2>&1 || true
  docker run -d --name nvis-head-relay --restart unless-stopped \
    -p "${RELAY_RW_PORT}:${RELAY_RW_PORT}" \
    -p "${RELAY_LOKI_PORT}:${RELAY_LOKI_PORT}" \
    -v /etc/nvis-relay/nginx.conf:/etc/nginx/nginx.conf:ro \
    "${NGINX_IMAGE}"
  echo ">> nvis-head-relay container started"
fi
rm -f "${rendered}"

cat <<EOF

Verify from a compute node (expect a TCP connect, not a hang):
  nc -z -w3 <RELAY_HOST> ${RELAY_RW_PORT}   && echo "remote_write relay reachable"
  nc -z -w3 <RELAY_HOST> ${RELAY_LOKI_PORT} && echo "loki relay reachable"
Then set on the nodes:  RELAY_HOST=<this head node>  and run install-node.sh.
EOF
