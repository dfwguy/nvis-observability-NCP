#!/usr/bin/env python3
"""UFM Telemetry CSV -> Prometheus exporter (pure stdlib, no deps).

Polls the OPEN UFM Telemetry CSV endpoint (http://<ufm>:9001/csv/metrics, no
auth) every POLL_INTERVAL seconds, resolves CSV column indices by NAME from the
header row, emits per-port switch fabric counters + switch-level env metrics,
and serves /metrics via http.server.

Design notes:
  * Only node_type==2 (switch) AND logical_state==4 (active) rows produce
    per-port series (large fabrics can reach tens of thousands of ports).
  * switch-level temp/power/voltage emit exactly 1 series/switch (dedupe by
    host_name).
  * 64-bit counters split into *_high/*_low 32-bit halves are recombined as
    value = (high << 32) | low.
  * eff_ber value -1 == no-data marker -> series dropped (only >=0 emitted).
  * On fetch/parse failure: ufm_telemetry_up=0, LAST-GOOD registry retained.
  * All socket writes wrapped in try/except to survive BrokenPipeError.

Environment variables (all site values come from the .env the installer writes):
  PORT                Listen port                          (default: 9105)
  CLUSTER_NAME        Prometheus cluster label             (default: cluster)
  POLL_INTERVAL       Seconds between polls                 (default: 90)
  FETCH_TIMEOUT       CSV fetch timeout seconds             (default: 60)
  UFM_HOST            UFM host — used to build the default CSV URL
  UFM_TELEMETRY_PORT  UFM telemetry CSV port                (default: 9001)
  UFM_CSV_URL         Full CSV endpoint URL override
                      (default: http://$UFM_HOST:$UFM_TELEMETRY_PORT/csv/metrics)
  SU_HOST_FILTER      Optional comma-separated host_name prefixes/exact names to
                      scope cardinality. Empty => all active switch ports.
"""
import os
import sys
import csv
import time
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "9105"))
CLUSTER = os.environ.get("CLUSTER_NAME", "cluster")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "90"))
UFM_HOST = os.environ.get("UFM_HOST", "").strip()
UFM_TELEMETRY_PORT = os.environ.get("UFM_TELEMETRY_PORT", "9001").strip()
# UFM_CSV_URL (or legacy UFM_TELEMETRY_URL) wins; otherwise derive from UFM_HOST.
UFM_CSV_URL = (
    os.environ.get("UFM_CSV_URL")
    or os.environ.get("UFM_TELEMETRY_URL")
    or (f"http://{UFM_HOST}:{UFM_TELEMETRY_PORT}/csv/metrics" if UFM_HOST else "")
)
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "60"))
# Optional SU scoping: comma-separated host_name prefixes or exact names.
# Empty => emit all active switch ports.
SU_HOST_FILTER = [s.strip() for s in
                  os.environ.get("SU_HOST_FILTER", "").split(",") if s.strip()]

# ----- CSV columns we need, resolved by NAME from the header row -----
# Single-column (direct value) fields.
SINGLE_COLS = [
    "timestamp", "source_id", "node_guid", "port_guid", "port_num",
    "aport", "port_type", "node_description", "host_name",
    "node_type", "logical_state",
    "SymbolErrorCounterExtended", "LinkErrorRecoveryCounterExtended",
    "LinkDownedCounterExtended", "PortRcvErrorsExtended",
    "PortRcvRemotePhysicalErrorsExtended", "PortRcvSwitchRelayErrorsExtended",
    "PortXmitDiscardsExtended", "LocalLinkIntegrityErrorsExtended",
    "VL15DroppedExtended", "PortXmitWaitExtended",
    "PortXmitDataExtended", "PortRcvDataExtended",
    "eeber",
    "switch_temperature", "switch_power_psu0", "switch_power_psu1",
    "switch_power_sum", "switch_voltage",
]
# 16 congestion VLs, summed into ufm_port_xmit_cong_time.
CONG_COLS = ["PortVLXmitTimeCongExt%d" % i for i in range(16)]
# 20 BER histogram bins hist0..hist19 -> phy_hist_bin{bin="N"}.
# Emitted SPARSELY (only non-zero bins) so an idle/healthy fabric adds ~0
# series; bins populate only when raw-BER errors accumulate (the high bins
# bin>=4 are the leading flap indicator the BER dashboard watches).
HIST_COLS = ["hist%d" % i for i in range(20)]
# 64-bit high/low pairs -> combined counters.
PAIR_COLS = {
    "phy_raw_errors_lane0": ("phy_c_edpl_bip_errors_lane0_high",
                             "phy_c_edpl_bip_errors_lane0_low"),
    "phy_raw_errors_lane1": ("phy_c_edpl_bip_errors_lane1_high",
                             "phy_c_edpl_bip_errors_lane1_low"),
    "phy_raw_errors_lane2": ("phy_c_edpl_bip_errors_lane2_high",
                             "phy_c_edpl_bip_errors_lane2_low"),
    "phy_raw_errors_lane3": ("phy_c_edpl_bip_errors_lane3_high",
                             "phy_c_edpl_bip_errors_lane3_low"),
    "phy_fec_corrected_blocks_total": ("phy_c_rs_fec_corrected_blocks_high",
                                       "phy_c_rs_fec_corrected_blocks_low"),
    "phy_fec_uncorrectable_blocks_total": (
        "phy_c_rs_fec_uncorrectable_blocks_high",
        "phy_c_rs_fec_uncorrectable_blocks_low"),
}

# Per-port raw int64 counters: metric_name == csv_column_name.
RAW_PORT_METRICS = [
    "SymbolErrorCounterExtended", "LinkErrorRecoveryCounterExtended",
    "LinkDownedCounterExtended", "PortRcvErrorsExtended",
    "PortRcvRemotePhysicalErrorsExtended", "PortRcvSwitchRelayErrorsExtended",
    "PortXmitDiscardsExtended", "LocalLinkIntegrityErrorsExtended",
    "VL15DroppedExtended", "PortXmitWaitExtended",
    "PortXmitDataExtended", "PortRcvDataExtended",
]

# Shared, atomically-swapped snapshot of rendered metrics text.
_lock = threading.Lock()
_rendered = "# no data yet\n"


def _esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(d):
    return ",".join('%s="%s"' % (k, _esc(v)) for k, v in d.items())


def _to_int(s):
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _to_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _combine(row, hi_idx, lo_idx):
    hi = _to_int(row[hi_idx]) if hi_idx is not None else None
    lo = _to_int(row[lo_idx]) if lo_idx is not None else None
    if hi is None and lo is None:
        return None
    return ((hi or 0) << 32) | (lo or 0)


def _keep_host(host):
    if not SU_HOST_FILTER:
        return True
    for f in SU_HOST_FILTER:
        if host == f or host.startswith(f):
            return True
    return False


def fetch_rows():
    """Yield (header_list, row_iterator) by streaming the CSV endpoint."""
    req = urllib.request.Request(UFM_CSV_URL)
    resp = urllib.request.urlopen(req, timeout=FETCH_TIMEOUT)
    # Stream line-by-line; never load the (potentially tens of MB) body whole.
    text_stream = (line.decode("utf-8", "replace") for line in resp)
    reader = csv.reader(text_stream)
    header = next(reader)
    return header, reader, resp


def scrape_once():
    """Fetch + parse; return (metrics_text, up, duration, ports_emitted)."""
    t0 = time.time()
    ports_emitted = 0
    lines = []

    if not UFM_CSV_URL:
        dur = time.time() - t0
        sys.stderr.write("UFM_CSV_URL/UFM_HOST not set — nothing to poll\n")
        return None, 0, dur, 0

    try:
        header, reader, resp = fetch_rows()
    except Exception as e:  # noqa: BLE001
        dur = time.time() - t0
        sys.stderr.write("fetch failed: %r\n" % (e,))
        return None, 0, dur, 0

    # Resolve indices by NAME.
    idx = {}
    for i, name in enumerate(header):
        idx[name.strip()] = i

    def gi(name):
        return idx.get(name)

    need = ["node_type", "logical_state", "host_name", "port_num",
            "source_id"]
    if any(gi(n) is None for n in need):
        resp.close()
        dur = time.time() - t0
        sys.stderr.write("header missing required cols: %s\n" %
                         [n for n in need if gi(n) is None])
        return None, 0, dur, 0

    i_ntype = gi("node_type")
    i_lstate = gi("logical_state")
    i_host = gi("host_name")
    i_port = gi("port_num")
    i_guid = gi("source_id")
    i_pguid = gi("port_guid")
    i_ptype = gi("port_type")
    i_ndesc = gi("node_description")
    i_eeber = gi("eeber")
    cong_idx = [gi(c) for c in CONG_COLS]
    hist_idx = [gi(c) for c in HIST_COLS]
    pair_idx = {m: (gi(h), gi(l)) for m, (h, l) in PAIR_COLS.items()}
    raw_idx = {m: gi(m) for m in RAW_PORT_METRICS}

    # switch-level env cols
    i_temp = gi("switch_temperature")
    i_ppsu0 = gi("switch_power_psu0")
    i_ppsu1 = gi("switch_power_psu1")
    i_psum = gi("switch_power_sum")
    i_volt = gi("switch_voltage")

    ncols = len(header)
    seen_switch = set()  # host_name dedupe for switch-level env
    # Accumulate metric families to keep HELP/TYPE grouped and valid.
    fam = {}

    def emit(metric, labels, value):
        fam.setdefault(metric, []).append("%s{%s} %s" % (
            metric, _labels(labels), repr(float(value))))

    try:
        for row in reader:
            if len(row) < ncols:
                continue
            if row[i_ntype].strip() != "2":
                continue  # switches only
            host = row[i_host].strip()
            if not _keep_host(host):
                continue

            # ---- switch-level env: 1 series/switch (dedupe host_name) ----
            if host and host not in seen_switch:
                seen_switch.add(host)
                slab = {"cluster": CLUSTER, "switch": host}
                if i_temp is not None:
                    v = _to_float(row[i_temp])
                    if v is not None:
                        emit("ufm_switch_temperature_celsius", slab, v)
                if i_volt is not None:
                    v = _to_float(row[i_volt])
                    if v is not None:
                        emit("ufm_switch_voltage_volts", slab, v)
                if i_psum is not None:
                    v = _to_float(row[i_psum])
                    if v is not None:
                        emit("ufm_switch_power_watts", slab, v)
                for psu_i, psu_name in ((i_ppsu0, "0"), (i_ppsu1, "1")):
                    if psu_i is not None:
                        v = _to_float(row[psu_i])
                        if v is not None:
                            pl = dict(slab)
                            pl["psu"] = psu_name
                            emit("ufm_switch_power_psu_watts", pl, v)

            # ---- per-port metrics: active ports only ----
            if row[i_lstate].strip() != "4":
                continue

            lab = {
                "cluster": CLUSTER,
                "switch": host,
                "port": row[i_port].strip(),
                "guid": row[i_guid].strip(),
            }
            if i_pguid is not None:
                lab["port_guid"] = row[i_pguid].strip()
            if i_ptype is not None:
                lab["port_type"] = row[i_ptype].strip()
            if i_ndesc is not None:
                lab["node_description"] = row[i_ndesc].strip()

            # raw int64 counters
            for m, ci in raw_idx.items():
                if ci is None:
                    continue
                v = _to_int(row[ci])
                if v is not None:
                    emit(m, lab, v)

            # congestion sum across 16 VLs
            csum = 0
            have_cong = False
            for ci in cong_idx:
                if ci is None:
                    continue
                v = _to_int(row[ci])
                if v is not None:
                    csum += v
                    have_cong = True
            if have_cong:
                emit("ufm_port_xmit_cong_time", lab, csum)

            # BER histogram bins — emit only non-zero (sparse) to cap cardinality
            for bi, ci in enumerate(hist_idx):
                if ci is None:
                    continue
                v = _to_int(row[ci])
                if v:  # non-zero only
                    hl = dict(lab)
                    hl["bin"] = str(bi)
                    emit("phy_hist_bin", hl, v)

            # 64-bit combined FEC / raw-error lanes
            for m, (hi, lo) in pair_idx.items():
                val = _combine(row, hi, lo)
                if val is not None:
                    emit(m, lab, val)

            # effective BER: drop -1 no-data marker
            if i_eeber is not None:
                v = _to_float(row[i_eeber])
                if v is not None and v >= 0:
                    emit("eff_ber", lab, v)

            ports_emitted += 1
    finally:
        resp.close()

    # HELP/TYPE headers per family
    for metric in sorted(fam):
        if metric == "eff_ber" or metric.startswith("ufm_switch_"):
            mtype = "gauge"
        else:
            mtype = "counter"
        lines.append("# TYPE %s %s" % (metric, mtype))
        lines.extend(fam[metric])

    dur = time.time() - t0
    return "\n".join(lines) + "\n", 1, dur, ports_emitted


def poller():
    global _rendered
    last_good_body = None
    while True:
        body, up, dur, ports = scrape_once()
        if up and body is not None:
            last_good_body = body
        else:
            body = last_good_body  # retain last good; may be None on 1st fail

        selfm = []
        selfm.append("# TYPE ufm_telemetry_up gauge")
        selfm.append('ufm_telemetry_up{cluster="%s"} %d' % (CLUSTER, up))
        selfm.append("# TYPE ufm_telemetry_scrape_duration_seconds gauge")
        selfm.append('ufm_telemetry_scrape_duration_seconds{cluster="%s"} %s'
                     % (CLUSTER, repr(dur)))
        selfm.append("# TYPE ufm_telemetry_ports_emitted gauge")
        selfm.append('ufm_telemetry_ports_emitted{cluster="%s"} %d'
                     % (CLUSTER, ports))
        selfm.append("# TYPE ufm_telemetry_last_scrape_timestamp_seconds gauge")
        selfm.append('ufm_telemetry_last_scrape_timestamp_seconds{cluster="%s"} %s'
                     % (CLUSTER, repr(time.time())))

        payload = "\n".join(selfm) + "\n"
        if body:
            payload += body

        with _lock:
            _rendered = payload

        sys.stderr.write(
            "scrape up=%d dur=%.2fs ports=%d\n" % (up, dur, ports))
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default logging
        pass

    def _write(self, code, body, ctype="text/plain; version=0.0.4"):
        try:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("write error: %r\n" % (e,))

    def do_GET(self):
        if self.path in ("/metrics", "/"):
            with _lock:
                body = _rendered
            self._write(200, body)
        elif self.path in ("/-/healthy", "/healthz"):
            self._write(200, "ok\n")
        else:
            self._write(404, "not found\n")


def main():
    t = threading.Thread(target=poller, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stderr.write("ufm-telemetry-exporter listening on :%d (url=%s)\n"
                     % (PORT, UFM_CSV_URL or "<unset>"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
