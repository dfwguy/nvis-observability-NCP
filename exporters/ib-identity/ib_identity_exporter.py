#!/usr/bin/env python3
"""
ib-identity exporter — serves the topology/identity JOIN BRIDGE metrics.

Stdlib-only HTTP server. On startup and on a refresh interval it (re)builds the
info-metric text from the ground-truth artifacts via
generate_identity.build_all(), and serves:

  GET /metrics          Prometheus text exposition (ib_switch_info, ib_link_peer,
                        ib_host_rail_leaf, ib_leaf_spine, ib_leaf_leaf,
                        ib_node_placement) + ib_identity_* meta metrics.
  GET /alias.json       host <-> system_name <-> guid <-> ip <-> rail <-> rack/su
  GET /blast-index.json compact adjacency for a Cytoscape topology iframe
  GET /healthz          liveness

Data is static per topology revision, so this is cheap: the rebuild is done in a
background cache; a scrape just serves bytes.

The ground-truth artifacts (expected-topology.json, placement_map.json,
ufm_switches_new.csv) are read from IB_IDENTITY_DATA (default ./data). This repo
ships tiny SAMPLE artifacts so the exporter runs out of the box; replace them
with your site's real files (mount a host directory over /app/data) to emit real
identity — see this exporter's README.

Environment variables:
  PORT             Listen port                         (default: 9118)
  REFRESH_SECONDS  Rebuild interval                     (default: 3600)
  CLUSTER_NAME     Prometheus cluster label             (default: cluster)
  FABRIC           Prometheus fabric label              (default: fabric)
  IB_IDENTITY_DATA Directory holding the ground-truth artifacts (default ./data)
  HOST_NAME_PREFIX Canonical host hostname prefix        (default: gpu)
"""

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import generate_identity as gen

PORT = int(os.environ.get("PORT", "9118"))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "3600"))
CLUSTER = os.environ.get("CLUSTER_NAME", "cluster")
FABRIC = os.environ.get("FABRIC", "fabric")


class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.metrics = b""
        self.alias = b"{}"
        self.blast = b"{}"
        self.built_at = 0.0
        self.counts = {}
        self.error = ""

    def rebuild(self):
        try:
            metrics_text, alias, blast, counts = gen.build_all()
            import json
            built = time.time()
            meta = [
                "# HELP ib_identity_build_timestamp_seconds Unix time of last identity rebuild.",
                "# TYPE ib_identity_build_timestamp_seconds gauge",
                'ib_identity_build_timestamp_seconds{{cluster="{}",fabric="{}"}} {:.0f}'.format(
                    CLUSTER, FABRIC, built),
                "# HELP ib_identity_series_total Series emitted per identity metric family.",
                "# TYPE ib_identity_series_total gauge",
            ]
            for name in sorted(counts):
                meta.append(
                    'ib_identity_series_total{{cluster="{}",fabric="{}",metric="{}"}} {}'.format(
                        CLUSTER, FABRIC, name, counts[name]))
            meta.append(
                "# HELP ib_identity_up 1 if the identity bridge built successfully.")
            meta.append("# TYPE ib_identity_up gauge")
            meta.append(
                'ib_identity_up{{cluster="{}",fabric="{}"}} 1'.format(CLUSTER, FABRIC))
            full = "\n".join(meta) + "\n" + metrics_text
            with self.lock:
                self.metrics = full.encode()
                self.alias = json.dumps(alias, separators=(",", ":"), sort_keys=True).encode()
                self.blast = json.dumps(blast, separators=(",", ":"), sort_keys=True).encode()
                self.built_at = built
                self.counts = counts
                self.error = ""
            sys.stderr.write("[ib-identity] rebuilt {} series across {} families\n".format(
                sum(counts.values()), len(counts)))
        except Exception as exc:  # keep serving last-good on failure
            with self.lock:
                self.error = str(exc)
            sys.stderr.write("[ib-identity] rebuild FAILED: {}\n".format(exc))


CACHE = Cache()


def _refresher():
    while True:
        time.sleep(REFRESH_SECONDS)
        CACHE.rebuild()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        with CACHE.lock:
            metrics, alias, blast, err = CACHE.metrics, CACHE.alias, CACHE.blast, CACHE.error
        if path in ("/metrics", "/"):
            if not metrics:
                self._send(503, b"# identity not built yet\n", "text/plain")
            else:
                self._send(200, metrics, "text/plain; version=0.0.4; charset=utf-8")
        elif path == "/alias.json":
            self._send(200, alias, "application/json")
        elif path == "/blast-index.json":
            self._send(200, blast, "application/json")
        elif path == "/healthz":
            ok = bool(metrics) and not err
            self._send(200 if ok else 503,
                       (b"ok\n" if ok else ("error: " + err + "\n").encode()),
                       "text/plain")
        else:
            self._send(404, b"not found\n", "text/plain")

    do_HEAD = do_GET


def main():
    CACHE.rebuild()
    threading.Thread(target=_refresher, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write("[ib-identity] serving on :{} (refresh {}s)\n".format(PORT, REFRESH_SECONDS))
    srv.serve_forever()


if __name__ == "__main__":
    main()
