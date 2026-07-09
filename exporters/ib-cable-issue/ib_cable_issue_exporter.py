#!/usr/bin/env python3
"""
NVIS ib-cable-issue-exporter  (job `ib-cable`, :9116)

Reads the IB fabric cable-issue / miswiring audit CSV(s) produced by the cable
audit scanner (see the cable-audit exporter) and exposes them as Prometheus
metrics for the NVIS IB network observability suite.

Input files (a watched directory, path override supported):
  - Main snapshot:  cable-issue_<YYYY-MM-DD>_<HH-MM-SS>.csv
        columns: Node Description, Timestamp, Rack, Unit, Issue,
                 Source Switch Port, Expected Neighbor, Discovered Neighbor
  - Repatch target: cable-issue_<YYYY-MM-DD>_Target.csv  (Wrong-neighbor + Wrong-port worklist)
  - Optional switch->SU map (from expected-topology.json) for `su` enrichment.

The main CSV mixes:
  * blank Issue rows          -> per-switch "audited/heartbeat" markers (EXCLUDED from ib_cable_issue;
                                 counted as the audited-switch denominator)
  * Unreachable-device rows   -> emitted separately as ib_cable_switch_unreachable (coverage alert)
  * real miswiring rows       -> Media Unplugged / Extra-cable / Wrong-neighbor / Wrong-port
                                 -> emitted as ib_cable_issue

Metrics
  ib_cable_issue{node,rack,unit,issue,source_port,expected_neighbor,discovered_neighbor,su,fabric}=1
  ib_cable_switch_unreachable{node,rack,unit,su,fabric}=1
  ib_cable_issue_total{issue}                     # per-category count (all non-blank incl. Unreachable)
  ib_cable_issue_by_switch{switch}                # active miswiring issues per source switch
  ib_cable_repatch_worklist{node,rack,unit,issue,source_port,expected_neighbor,discovered_neighbor}=1
  ib_cable_repatch_worklist_total{issue}
  ib_cable_switch_unreachable_total
  ib_cable_audited_switches_total                 # distinct switches with a heartbeat row (denominator)
  ib_cable_active_issues_total                    # sum of the 4 miswiring categories
  ib_cable_snapshot_timestamp                     # unix seconds of the snapshot (from filename/Timestamp)
  ib_cable_target_snapshot_timestamp
  ib_cable_scrape_success                         # 1 if last refresh parsed OK else 0
  ib_cable_last_refresh_timestamp

Environment (all site values come from the .env the installer writes)
  CABLE_PORT              listen port (default 9116)
  CABLE_WATCH_DIR         dir scanned for cable-issue CSVs (default /data)
  CABLE_ISSUE_CSV         explicit main CSV path override (else newest cable-issue_*_*.csv in WATCH_DIR)
  CABLE_TARGET_CSV        explicit target CSV path override (else newest *_Target.csv in WATCH_DIR)
  SWITCH_MAP_JSON         switch->{su,role,rack,unit} map for enrichment (default /data/switch_su_map.json)
  FABRIC                  fabric label (default fabric)
  REFRESH_INTERVAL        seconds between file re-reads (default 60)
  LOKI_URL                optional Loki push URL (e.g. http://loki:3100); best-effort, disabled if unset
"""
import glob
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, GaugeMetricFamily

log = logging.getLogger("ib-cable")

# ---- config -----------------------------------------------------------------
PORT = int(os.environ.get("CABLE_PORT", "9116"))
WATCH_DIR = os.environ.get("CABLE_WATCH_DIR", "/data")
ISSUE_CSV_OVERRIDE = os.environ.get("CABLE_ISSUE_CSV", "")
TARGET_CSV_OVERRIDE = os.environ.get("CABLE_TARGET_CSV", "")
SWITCH_MAP_JSON = os.environ.get("SWITCH_MAP_JSON", os.path.join(WATCH_DIR, "switch_su_map.json"))
FABRIC = os.environ.get("FABRIC", "fabric")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "60"))
LOKI_URL = os.environ.get("LOKI_URL", "").rstrip("/")

# Issue categories that represent real miswiring (emitted as ib_cable_issue).
MISWIRE_ISSUES = {"Media Unplugged", "Extra-cable", "Wrong-neighbor", "Wrong-port"}
UNREACHABLE_ISSUE = "Unreachable-device"

_TS_FROM_NAME = re.compile(r"cable-issue_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")


def _load_switch_map(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("switch map %s not loaded: %s", path, exc)
        return {}


def _pick_latest(watch_dir, target=False):
    """Newest cable-issue CSV in watch_dir. target=True -> *_Target.csv, else the timestamped snapshot."""
    if target:
        cands = glob.glob(os.path.join(watch_dir, "cable-issue_*_Target.csv"))
    else:
        cands = [
            p for p in glob.glob(os.path.join(watch_dir, "cable-issue_*.csv"))
            if not p.endswith("_Target.csv")
        ]
    if not cands:
        return ""
    # Prefer lexical max (filenames are timestamped) then mtime as tiebreaker.
    return max(cands, key=lambda p: (os.path.basename(p), os.path.getmtime(p)))


def _parse_ts(name, first_row_ts):
    """Snapshot unix seconds: from the filename timestamp if present, else the first row Timestamp."""
    m = _TS_FROM_NAME.search(os.path.basename(name or ""))
    if m:
        try:
            dt = datetime.strptime(
                f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    if first_row_ts:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%y %H:%M", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(first_row_ts.strip(), fmt).replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                continue
    return 0.0


def _read_csv(path):
    """Read a cable-issue CSV robustly (utf-8-sig strips the BOM). Returns list of dict rows."""
    import csv

    rows = []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    return rows


class CableIssueState:
    """Parsed snapshot, refreshed by a background thread; served on each Prometheus scrape."""

    def __init__(self):
        self.lock = threading.Lock()
        self.issue_rows = []        # miswiring rows (dict)
        self.unreachable_rows = []  # unreachable switch rows (dict)
        self.worklist_rows = []     # target repatch rows (dict)
        self.by_issue = {}          # issue -> count (all non-blank incl unreachable)
        self.by_switch = {}         # switch -> miswiring-issue count
        self.worklist_by_issue = {}
        self.audited_switches = 0
        self.snapshot_ts = 0.0
        self.target_ts = 0.0
        self.refresh_ts = 0.0
        self.ok = 0
        self.switch_map = _load_switch_map(SWITCH_MAP_JSON)
        self._loki_sent_sig = None

    def _su(self, node):
        ent = self.switch_map.get(node)
        return ent.get("su", "") if isinstance(ent, dict) else ""

    def refresh(self):
        issue_csv = ISSUE_CSV_OVERRIDE or _pick_latest(WATCH_DIR, target=False)
        target_csv = TARGET_CSV_OVERRIDE or _pick_latest(WATCH_DIR, target=True)
        try:
            main_rows = _read_csv(issue_csv) if issue_csv else []
        except Exception as exc:  # noqa: BLE001
            log.error("failed reading main CSV %s: %s", issue_csv, exc)
            with self.lock:
                self.ok = 0
                self.refresh_ts = time.time()
            return

        issue_rows, unreachable_rows = [], []
        by_issue, by_switch = {}, {}
        audited = set()
        first_ts = ""
        for r in main_rows:
            node = r.get("Node Description", "")
            issue = r.get("Issue", "")
            if not first_ts:
                first_ts = r.get("Timestamp", "")
            if issue == "":
                # blank heartbeat row: this switch was reachable & audited clean-ish
                if node:
                    audited.add(node)
                continue
            by_issue[issue] = by_issue.get(issue, 0) + 1
            rec = {
                "node": node,
                "rack": r.get("Rack", ""),
                "unit": r.get("Unit", ""),
                "issue": issue,
                "source_port": r.get("Source Switch Port", ""),
                "expected_neighbor": r.get("Expected Neighbor", ""),
                "discovered_neighbor": r.get("Discovered Neighbor", ""),
                "su": self._su(node),
            }
            if issue == UNREACHABLE_ISSUE:
                unreachable_rows.append(rec)
            elif issue in MISWIRE_ISSUES:
                issue_rows.append(rec)
                by_switch[node] = by_switch.get(node, 0) + 1
            else:
                # unknown future category -> still surface as an issue row
                issue_rows.append(rec)
                by_switch[node] = by_switch.get(node, 0) + 1

        # target / repatch worklist
        worklist_rows, worklist_by_issue = [], {}
        target_first_ts = ""
        try:
            for r in (_read_csv(target_csv) if target_csv else []):
                issue = r.get("Issue", "")
                if not target_first_ts:
                    target_first_ts = r.get("Timestamp", "")
                if issue == "":
                    continue
                worklist_by_issue[issue] = worklist_by_issue.get(issue, 0) + 1
                worklist_rows.append({
                    "node": r.get("Node Description", ""),
                    "rack": r.get("Rack", ""),
                    "unit": r.get("Unit", ""),
                    "issue": issue,
                    "source_port": r.get("Source Switch Port", ""),
                    "expected_neighbor": r.get("Expected Neighbor", ""),
                    "discovered_neighbor": r.get("Discovered Neighbor", ""),
                })
        except Exception as exc:  # noqa: BLE001
            log.warning("failed reading target CSV %s: %s", target_csv, exc)

        snap_ts = _parse_ts(issue_csv, first_ts)
        tgt_ts = _parse_ts(target_csv, target_first_ts)

        with self.lock:
            self.issue_rows = issue_rows
            self.unreachable_rows = unreachable_rows
            self.worklist_rows = worklist_rows
            self.by_issue = by_issue
            self.by_switch = by_switch
            self.worklist_by_issue = worklist_by_issue
            self.audited_switches = len(audited)
            self.snapshot_ts = snap_ts
            self.target_ts = tgt_ts
            self.refresh_ts = time.time()
            self.ok = 1

        log.info(
            "refresh ok: main=%s target=%s | issues=%d unreachable=%d worklist=%d audited=%d by_issue=%s",
            os.path.basename(issue_csv), os.path.basename(target_csv),
            len(issue_rows), len(unreachable_rows), len(worklist_rows),
            len(audited), by_issue,
        )
        self._maybe_push_loki(issue_rows, unreachable_rows, snap_ts)

    def _maybe_push_loki(self, issue_rows, unreachable_rows, snap_ts):
        """Best-effort push of one log line per issue to Loki (only when the snapshot changes)."""
        if not LOKI_URL:
            return
        sig = (snap_ts, len(issue_rows), len(unreachable_rows))
        if sig == self._loki_sent_sig:
            return
        try:
            import urllib.request

            ns = str(int((snap_ts or time.time()) * 1e9))
            streams = {}
            for rec in issue_rows + unreachable_rows:
                key = (rec["issue"], rec["node"], rec.get("su", ""))
                line = (
                    f'issue="{rec["issue"]}" switch={rec["node"]} port={rec.get("source_port","")} '
                    f'expected="{rec.get("expected_neighbor","")}" discovered="{rec.get("discovered_neighbor","")}" '
                    f'rack={rec.get("rack","")} unit={rec.get("unit","")}'
                )
                streams.setdefault(key, []).append([ns, line])
            payload = {"streams": [
                {"stream": {"job": "ib-cable", "issue": k[0], "source_switch": k[1],
                            "su": k[2], "fabric": FABRIC},
                 "values": v}
                for k, v in streams.items()
            ]}
            req = urllib.request.Request(
                f"{LOKI_URL}/loki/api/v1/push",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()
            self._loki_sent_sig = sig
            log.info("pushed %d cable-issue lines to Loki", sum(len(v) for v in streams.values()))
        except Exception as exc:  # noqa: BLE001
            log.warning("Loki push failed (non-fatal): %s", exc)


class CableIssueCollector:
    def __init__(self, state: CableIssueState):
        self.state = state

    def collect(self):
        st = self.state
        with st.lock:
            issue_rows = list(st.issue_rows)
            unreachable_rows = list(st.unreachable_rows)
            worklist_rows = list(st.worklist_rows)
            by_issue = dict(st.by_issue)
            by_switch = dict(st.by_switch)
            worklist_by_issue = dict(st.worklist_by_issue)
            audited = st.audited_switches
            snap_ts = st.snapshot_ts
            tgt_ts = st.target_ts
            refresh_ts = st.refresh_ts
            ok = st.ok

        issue_labels = ["node", "rack", "unit", "issue", "source_port",
                        "expected_neighbor", "discovered_neighbor", "su", "fabric"]
        g_issue = GaugeMetricFamily(
            "ib_cable_issue",
            "Active IB cable miswiring issue (1 per audited problem port; excludes clean/unreachable rows)",
            labels=issue_labels,
        )
        for r in issue_rows:
            g_issue.add_metric(
                [r["node"], r["rack"], r["unit"], r["issue"], r["source_port"],
                 r["expected_neighbor"], r["discovered_neighbor"], r.get("su", ""), FABRIC], 1,
            )
        yield g_issue

        g_unreach = GaugeMetricFamily(
            "ib_cable_switch_unreachable",
            "IB switch unreachable during cable audit (coverage gap, not a clean switch)",
            labels=["node", "rack", "unit", "su", "fabric"],
        )
        for r in unreachable_rows:
            g_unreach.add_metric([r["node"], r["rack"], r["unit"], r.get("su", ""), FABRIC], 1)
        yield g_unreach

        g_total = GaugeMetricFamily(
            "ib_cable_issue_total",
            "Count of cable-audit rows per issue category (all non-blank, includes Unreachable-device)",
            labels=["issue"],
        )
        for issue, n in by_issue.items():
            g_total.add_metric([issue], n)
        yield g_total

        g_by_sw = GaugeMetricFamily(
            "ib_cable_issue_by_switch",
            "Active miswiring issue count per source switch",
            labels=["switch", "su", "fabric"],
        )
        for sw, n in by_switch.items():
            su = st.switch_map.get(sw, {}).get("su", "") if isinstance(st.switch_map.get(sw), dict) else ""
            g_by_sw.add_metric([sw, su, FABRIC], n)
        yield g_by_sw

        g_work = GaugeMetricFamily(
            "ib_cable_repatch_worklist",
            "Actionable re-patch worklist row from the Target CSV (Wrong-neighbor + Wrong-port)",
            labels=["node", "rack", "unit", "issue", "source_port",
                    "expected_neighbor", "discovered_neighbor"],
        )
        for r in worklist_rows:
            g_work.add_metric(
                [r["node"], r["rack"], r["unit"], r["issue"], r["source_port"],
                 r["expected_neighbor"], r["discovered_neighbor"]], 1,
            )
        yield g_work

        g_work_total = GaugeMetricFamily(
            "ib_cable_repatch_worklist_total",
            "Re-patch worklist row count per issue category",
            labels=["issue"],
        )
        for issue, n in worklist_by_issue.items():
            g_work_total.add_metric([issue], n)
        yield g_work_total

        g_unreach_total = GaugeMetricFamily(
            "ib_cable_switch_unreachable_total",
            "Number of switches unreachable during the cable audit",
        )
        g_unreach_total.add_metric([], len(unreachable_rows))
        yield g_unreach_total

        g_audited = GaugeMetricFamily(
            "ib_cable_audited_switches_total",
            "Distinct switches with a heartbeat (audited) row in the snapshot (denominator)",
        )
        g_audited.add_metric([], audited)
        yield g_audited

        g_active = GaugeMetricFamily(
            "ib_cable_active_issues_total",
            "Total active miswiring issues emitted as ib_cable_issue",
        )
        g_active.add_metric([], len(issue_rows))
        yield g_active

        g_snap = GaugeMetricFamily(
            "ib_cable_snapshot_timestamp",
            "Unix time of the cable-issue snapshot currently loaded",
        )
        g_snap.add_metric([], snap_ts)
        yield g_snap

        g_tsnap = GaugeMetricFamily(
            "ib_cable_target_snapshot_timestamp",
            "Unix time of the repatch-target snapshot currently loaded",
        )
        g_tsnap.add_metric([], tgt_ts)
        yield g_tsnap

        g_ok = GaugeMetricFamily(
            "ib_cable_scrape_success",
            "1 if the last file refresh parsed successfully, else 0",
        )
        g_ok.add_metric([], ok)
        yield g_ok

        g_refresh = GaugeMetricFamily(
            "ib_cable_last_refresh_timestamp",
            "Unix time of the last successful file refresh",
        )
        g_refresh.add_metric([], refresh_ts)
        yield g_refresh


def _refresh_loop(state: CableIssueState):
    while True:
        try:
            state.refresh()
        except Exception as exc:  # noqa: BLE001
            log.exception("refresh loop error: %s", exc)
        time.sleep(REFRESH_INTERVAL)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state = CableIssueState()
    state.refresh()  # prime before serving so the first scrape has data
    REGISTRY.register(CableIssueCollector(state))
    t = threading.Thread(target=_refresh_loop, args=(state,), daemon=True)
    t.start()
    start_http_server(PORT)
    log.info("ib-cable-issue-exporter listening on :%d (watch_dir=%s, refresh=%ds, loki=%s)",
             PORT, WATCH_DIR, REFRESH_INTERVAL, LOKI_URL or "disabled")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
