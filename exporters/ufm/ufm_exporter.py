#!/usr/bin/env python3
"""
NVIS UFM Prometheus Exporter — v1.0
=====================================
Polls the NVIDIA UFM Enterprise REST API and exposes InfiniBand fabric metrics
on port 9103 in Prometheus text format.

Collected via UFM REST API (/ufmRest/):
  - Fabric version and health
  - All switch modules: name, GUID, temperature, firmware version, fan health
  - All ports: state, physical state, utilization %, error counters
  - All links: state, speed, width
  - Active alarms: severity, description, source
  - Events (last N, for rate metrics): type, severity, source
  - Cluster Minder plugin (if UFM_CLUSTER_MINDER=true):
      boot sequence per node, firmware compliance, fan health per switch

Environment variables (all site values come from the .env the installer writes —
nothing is hardcoded):
  UFM_HOST            UFM server IP or hostname          (required)
  UFM_USER            UFM username                       (default: ufmsystem)
  UFM_PASSWORD        UFM password                       (required; UFM_PASS also accepted)
  UFM_CLUSTER_MINDER  true/false — poll Cluster Minder plugin (default: false)
  CLUSTER_NAME        Prometheus label for the cluster   (default: cluster)
  PORT                Listen port                        (default: 9103)
  SCRAPE_INTERVAL     Seconds between polls               (default: 30)
  VERIFY_SSL          true/false                          (default: false)
  UFM_TIMEOUT         Per-request timeout seconds         (default: 15)

Usage (standalone):
  UFM_HOST=<ufm-ip> UFM_USER=ufmsystem UFM_PASSWORD=<pw> \
  CLUSTER_NAME=my-cluster python3 ufm_exporter.py

Usage (docker-compose) — see the ufm-exporter service in docker-compose.fabric.yml.
"""

import os
import sys
import time
import json
import ssl
import base64
import threading
import logging
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ufm-exporter")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UFM_HOST          = os.environ.get("UFM_HOST",          "")
UFM_USER          = os.environ.get("UFM_USER",          "ufmsystem")
# UFM_PASSWORD is the canonical name; UFM_PASS is accepted for backward-compat.
UFM_PASS          = os.environ.get("UFM_PASSWORD") or os.environ.get("UFM_PASS", "")
UFM_CLUSTER_MINDER = os.environ.get("UFM_CLUSTER_MINDER","false").lower() == "true"
CLUSTER_NAME      = os.environ.get("CLUSTER_NAME",      "cluster")
PORT              = int(os.environ.get("PORT",           "9103"))
SCRAPE_INTERVAL   = int(os.environ.get("SCRAPE_INTERVAL","30"))
VERIFY_SSL        = os.environ.get("VERIFY_SSL",         "false").lower() == "true"
TIMEOUT           = int(os.environ.get("UFM_TIMEOUT",    "15"))

# Max events to fetch for rate metrics
MAX_EVENTS = 1000


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def ufm_get(path: str):
    url = f"https://{UFM_HOST}{path}"
    ctx = ssl.create_default_context()
    if not VERIFY_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    token = base64.b64encode(f"{UFM_USER}:{UFM_PASS}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}",
        "Accept":        "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=TIMEOUT) as resp:
            raw = resp.read()
            if not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 501, 405):
            log.debug("UFM %s → HTTP %d", path, e.code)
        return None
    except Exception as e:
        log.debug("UFM %s → %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
def collect() -> dict:
    data = {
        "up":            False,
        "version":       "",
        "fabric_healthy": False,
        "modules":       [],   # switches
        "ports":         [],
        "links":         [],
        "alarms":        [],
        "events":        [],
        "cm":            {},   # Cluster Minder data
    }

    # ── Version / health check ───────────────────────────────────────────────
    ver = ufm_get("/ufmRest/app/ufm_version")
    if not ver:
        return data
    data["up"]      = True
    data["version"] = ver.get("ufm_release_version", ver.get("ufm_release", ver.get("version", "")))

    # ── Modules (switches) via systems endpoint ──────────────────────────────
    # UFM Enterprise 6.x: /ufmRest/resources/systems?type=switch
    modules = ufm_get("/ufmRest/resources/systems?type=switch") or []
    for m in (modules if isinstance(modules, list) else []):
        data["modules"].append({
            "name":          m.get("name", ""),
            "guid":          m.get("system_id", m.get("guid", "")),
            "description":   m.get("description", ""),
            "state":         m.get("state", ""),
            "severity":      m.get("severity", ""),
            "ip":            m.get("ip_addr", m.get("ip", "")),
            "fw_version":    m.get("fw_version", m.get("firmware_version", "")),
            "hw_revision":   m.get("hw_revision", ""),
            "num_of_ports":  int(m.get("num_of_ports", 0)),
            "type":          m.get("type", ""),
            "role":          m.get("role", ""),
            "total_alarms":  int(m.get("total_alarms", 0)),
            "temperature":   m.get("temperature", ""),
            "uptime":        m.get("uptime", ""),
            "model":         m.get("model", ""),
        })

    # Derive fabric health: healthy if UFM is up and no CRITICAL severity switches
    critical_switches = sum(1 for m in data["modules"]
                            if m["severity"].upper() == "CRITICAL")
    data["fabric_healthy"] = (len(data["modules"]) > 0 and critical_switches == 0)

    # ── Ports ────────────────────────────────────────────────────────────────
    ports = ufm_get("/ufmRest/resources/ports") or []
    for p in (ports if isinstance(ports, list) else []):
        state     = p.get("logical_state", p.get("state", ""))
        phys_state = p.get("physical_state", "")
        active    = 1 if state.upper() in ("ACTIVE", "UP") else 0
        ber_raw = p.get("high_ber_severity", "N/A")
        data["ports"].append({
            "guid":        p.get("systemID", ""),
            "port_num":    str(p.get("portNumber", p.get("port_num", p.get("number", "")))),
            "name":        p.get("name", ""),
            "state":       state,
            "phys_state":  phys_state,
            "active":      active,
            "speed":       p.get("active_speed", p.get("speed", "")),
            "width":       str(p.get("active_width", p.get("width", ""))),
            "module_name": p.get("module_name", p.get("system_name", "")),
            "peer_guid":   p.get("peerSystemID", ""),
            "peer_port":   str(p.get("peerPortNumber", "")),
            "tier":        int(p.get("tier", 0)),
            "severity":    p.get("severity", ""),
            "high_ber":    0 if str(ber_raw).upper() in ("N/A", "", "NONE") else 1,
            "peer_node":   p.get("peer_node_description", p.get("peer_node_name", "")),
        })

    # ── Port statistics (utilization + errors) ───────────────────────────────
    # POST to get stats for all ports at once (UFM v6+)
    stats_payload = json.dumps({
        "port_num": "all",
        "counter_categories": ["PortXmitData", "PortRcvData",
                               "PortXmitPkts", "PortRcvPkts",
                               "PortRcvErrors", "PortXmitDiscards",
                               "PortRcvRemotePhysicalErrors"]
    }).encode()
    stats_req = urllib.request.Request(
        f"https://{UFM_HOST}/ufmRest/reports/stats/getPortsStats",
        data=stats_payload,
        headers={
            "Authorization": f"Basic {base64.b64encode(f'{UFM_USER}:{UFM_PASS}'.encode()).decode()}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        },
        method="POST",
    )
    ctx_stats = ssl.create_default_context()
    if not VERIFY_SSL:
        ctx_stats.check_hostname = False
        ctx_stats.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(stats_req, context=ctx_stats, timeout=TIMEOUT) as resp:
            port_stats = json.loads(resp.read())
    except Exception:
        port_stats = {}

    # Attach stats to port records by GUID+port
    stats_map = {}
    for s in (port_stats.get("PortStats", []) if isinstance(port_stats, dict) else []):
        key = f"{s.get('systemID','')}:{s.get('portNumber','')}"
        stats_map[key] = s
    for p in data["ports"]:
        key = f"{p['guid']}:{p['port_num']}"
        s   = stats_map.get(key, {})
        p["xmit_data"]    = s.get("PortXmitData",    None)
        p["rcv_data"]     = s.get("PortRcvData",     None)
        p["xmit_pkts"]    = s.get("PortXmitPkts",    None)
        p["rcv_pkts"]     = s.get("PortRcvPkts",     None)
        p["rcv_errors"]   = s.get("PortRcvErrors",   None)
        p["xmit_discards"]= s.get("PortXmitDiscards",None)
        p["rcv_phys_errs"]= s.get("PortRcvRemotePhysicalErrors", None)

    # ── Links ────────────────────────────────────────────────────────────────
    links = ufm_get("/ufmRest/resources/links") or []
    for lk in (links if isinstance(links, list) else []):
        data["links"].append({
            "link_id":        lk.get("link_id", ""),
            "state":          lk.get("state", ""),
            "speed":          lk.get("speed", ""),
            "width":          str(lk.get("width", "")),
            "node_guid_a":    lk.get("source_system_id", ""),
            "port_num_a":     str(lk.get("source_port_num", "")),
            "node_guid_b":    lk.get("destination_system_id", ""),
            "port_num_b":     str(lk.get("destination_port_num", "")),
            "active":         1 if lk.get("state", "").upper() == "ACTIVE" else 0,
        })

    # ── Alarms ───────────────────────────────────────────────────────────────
    # UFM Enterprise 6.x: /ufmRest/app/alarms
    alarms = ufm_get("/ufmRest/app/alarms") or []
    for a in (alarms if isinstance(alarms, list) else []):
        data["alarms"].append({
            "id":          str(a.get("id", "")),
            "severity":    a.get("severity", ""),
            "description": a.get("description", ""),
            "source":      a.get("object_name", a.get("source_id", a.get("source", ""))),
            "event_type":  a.get("event_type", a.get("type", "")),
            "alarm_type":  a.get("name", a.get("type", "")),
            "reason":      a.get("reason", ""),
            "object_path": a.get("object_path", ""),
            "event_count": int(a.get("event_count", 1)),
        })

    # ── Events (recent) ──────────────────────────────────────────────────────
    # UFM Enterprise 6.x: /ufmRest/app/events
    events = ufm_get(f"/ufmRest/app/events") or []
    for e in (events if isinstance(events, list) else [])[:MAX_EVENTS]:
        data["events"].append({
            "id":         str(e.get("id", "")),
            "type":       e.get("name", e.get("type", e.get("event_type", ""))),
            "severity":   e.get("severity", ""),
            "source":     e.get("object_name", e.get("source_id", e.get("source", ""))),
            "category":   e.get("category", ""),
            "timestamp":  e.get("timestamp", ""),
        })

    # ── Cluster Minder plugin ────────────────────────────────────────────────
    if UFM_CLUSTER_MINDER:
        cm_status = ufm_get("/ufmRest/plugin/cluster_minder/api/v1/status") or {}
        cm_nodes  = ufm_get("/ufmRest/plugin/cluster_minder/api/v1/nodes") or []
        cm_events = ufm_get("/ufmRest/plugin/cluster_minder/api/v1/events?limit=200") or []
        data["cm"] = {
            "status":  cm_status,
            "nodes":   cm_nodes if isinstance(cm_nodes, list) else [],
            "events":  cm_events if isinstance(cm_events, list) else [],
        }

    return data


# ---------------------------------------------------------------------------
# Metrics renderer
# ---------------------------------------------------------------------------
def esc(s) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


SEV_SCORE = {"CRITICAL": 4, "ERROR": 3, "WARNING": 2, "INFO": 1, "NOTICE": 1}


def render_metrics(d: dict) -> str:
    from collections import Counter
    c = CLUSTER_NAME
    lines = []

    def add(metric, labels, value):
        lstr = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
        lines.append(f"{metric}{{{lstr}}} {value}")

    def hdr(help_text, mtype, name):
        lines.append(f"\n# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")

    # ── UFM up ────────────────────────────────────────────────────────────────
    hdr("1 if UFM REST API is reachable", "gauge", "ufm_up")
    add("ufm_up", {"cluster": c, "ufm_host": UFM_HOST}, 1 if d["up"] else 0)

    if not d["up"]:
        return "\n".join(lines) + "\n"

    hdr("UFM version info — value always 1", "gauge", "ufm_version_info")
    add("ufm_version_info", {"cluster": c, "version": d["version"]}, 1)

    hdr("1 if overall fabric health is OK", "gauge", "ufm_fabric_healthy")
    add("ufm_fabric_healthy", {"cluster": c}, 1 if d["fabric_healthy"] else 0)

    # ── Switches ──────────────────────────────────────────────────────────────
    hdr("Total number of switches in the fabric", "gauge", "ufm_switch_count")
    add("ufm_switch_count", {"cluster": c}, len(d["modules"]))

    # Switch counts by role (TOR/leaf vs CORE/spine)
    hdr("Number of switches by role", "gauge", "ufm_switch_count_by_role")
    role_counts = Counter(m["role"].upper() if m["role"] else "UNKNOWN" for m in d["modules"])
    for role, cnt in role_counts.items():
        add("ufm_switch_count_by_role", {"cluster": c, "role": role}, cnt)

    hdr("1 if switch is in Active/OK state", "gauge", "ufm_switch_up")
    hdr("Switch firmware version info — value 1", "gauge", "ufm_switch_firmware_info")
    hdr("Number of ports on the switch", "gauge", "ufm_switch_port_count")
    hdr("Switch severity as numeric score (4=Critical,3=Error,2=Warning,1=Info,0=OK)",
        "gauge", "ufm_switch_severity")
    hdr("Number of active alarms on the switch", "gauge", "ufm_switch_alarm_count")
    hdr("Switch temperature (Celsius)", "gauge", "ufm_switch_temperature_celsius")
    hdr("Switch uptime in seconds", "gauge", "ufm_switch_uptime_seconds")

    for m in d["modules"]:
        lbl = {"cluster": c, "switch": m["name"], "guid": m["guid"],
               "type": m["type"], "role": m["role"]}
        add("ufm_switch_up",        {**lbl}, 1 if m["state"].upper() in ("ACTIVE","OK") else 0)
        add("ufm_switch_port_count", {**lbl}, m["num_of_ports"])
        add("ufm_switch_severity",   {**lbl}, SEV_SCORE.get(m["severity"].upper(), 0))
        add("ufm_switch_alarm_count",{**lbl}, m["total_alarms"])
        if m["fw_version"]:
            add("ufm_switch_firmware_info",
                {**lbl, "fw_version": m["fw_version"], "hw_revision": m["hw_revision"]}, 1)
        # Temperature: UFM may return string like "32 C" or a number
        temp = m["temperature"]
        if temp:
            try:
                temp_val = float(str(temp).split()[0])
                add("ufm_switch_temperature_celsius", {**lbl}, temp_val)
            except (ValueError, IndexError):
                pass
        # Uptime: UFM returns string like "5 days, 3:12:44" or seconds int
        uptime = m.get("uptime", "")
        if uptime:
            try:
                # Try int first (seconds)
                add("ufm_switch_uptime_seconds", {**lbl}, int(uptime))
            except (ValueError, TypeError):
                try:
                    # Parse "X days, H:M:S" format
                    import re as _re
                    days = int((_re.search(r'(\d+)\s*day', str(uptime)) or type('', (), {'group': lambda *a: 0})()).group(1) or 0)
                    hms  = _re.search(r'(\d+):(\d+):(\d+)', str(uptime))
                    if hms:
                        h, mi, s = int(hms.group(1)), int(hms.group(2)), int(hms.group(3))
                        add("ufm_switch_uptime_seconds", {**lbl},
                            days * 86400 + h * 3600 + mi * 60 + s)
                except Exception:
                    pass

    # ── Ports ─────────────────────────────────────────────────────────────────
    active_ports = sum(p["active"] for p in d["ports"])
    total_ports  = len(d["ports"])
    hdr("Total IB ports tracked by UFM",   "gauge", "ufm_port_count")
    hdr("Active IB ports",                 "gauge", "ufm_port_active_count")
    add("ufm_port_count",        {"cluster": c}, total_ports)
    add("ufm_port_active_count", {"cluster": c}, active_ports)

    hdr("Port count by fabric tier (1=host-facing, 2=leaf, 3=spine, 4=core)",
        "gauge", "ufm_port_count_by_tier")
    tier_counts = Counter(str(p["tier"]) for p in d["ports"])
    for tier, cnt in tier_counts.items():
        add("ufm_port_count_by_tier", {"cluster": c, "tier": tier}, cnt)

    hdr("Active port count by link speed", "gauge", "ufm_port_count_by_speed")
    speed_counts = Counter(p["speed"] or "Unknown" for p in d["ports"] if p["active"])
    for speed, cnt in speed_counts.items():
        add("ufm_port_count_by_speed", {"cluster": c, "speed": speed}, cnt)

    hdr("Port count by physical state (Link Up / Polling / Down)",
        "gauge", "ufm_port_count_by_phys_state")
    phys_counts = Counter(p["phys_state"] or "Unknown" for p in d["ports"])
    for phys, cnt in phys_counts.items():
        add("ufm_port_count_by_phys_state", {"cluster": c, "phys_state": phys}, cnt)

    hdr("1 if port is logically active", "gauge", "ufm_port_up")
    hdr("Fabric tier of the port (1=host-facing,2=leaf,3=spine,4=core)",
        "gauge", "ufm_port_tier")
    hdr("Port severity score (4=Critical,3=Error,2=Warning,1=Info,0=OK)",
        "gauge", "ufm_port_severity")
    hdr("1 if port has high BER alarm active", "gauge", "ufm_port_high_ber")
    hdr("Port transmit data bytes (counter)", "counter", "ufm_port_xmit_data_bytes_total")
    hdr("Port receive data bytes (counter)",  "counter", "ufm_port_rcv_data_bytes_total")
    hdr("Port receive errors (counter)",      "counter", "ufm_port_rcv_errors_total")
    hdr("Port transmit discards (counter)",   "counter", "ufm_port_xmit_discards_total")

    for p in d["ports"]:
        lbl = {"cluster": c, "switch": p["module_name"], "port": p["port_num"],
               "speed": p["speed"] or "Unknown", "width": p["width"],
               "tier": str(p["tier"])}
        add("ufm_port_up",       lbl, p["active"])
        add("ufm_port_tier",     lbl, p["tier"])
        add("ufm_port_severity", lbl, SEV_SCORE.get(p.get("severity", "").upper(), 0))
        if p.get("high_ber"):
            add("ufm_port_high_ber", {**lbl, "peer": p.get("peer_node", "")}, 1)
        if p.get("xmit_data")    is not None:
            add("ufm_port_xmit_data_bytes_total",  lbl, p["xmit_data"])
        if p.get("rcv_data")     is not None:
            add("ufm_port_rcv_data_bytes_total",   lbl, p["rcv_data"])
        if p.get("rcv_errors")   is not None:
            add("ufm_port_rcv_errors_total",       lbl, p["rcv_errors"])
        if p.get("xmit_discards")is not None:
            add("ufm_port_xmit_discards_total",    lbl, p["xmit_discards"])

    # ── Links ─────────────────────────────────────────────────────────────────
    hdr("Total links in fabric",   "gauge", "ufm_link_count")
    hdr("Active links in fabric",  "gauge", "ufm_link_active_count")
    add("ufm_link_count",        {"cluster": c}, len(d["links"]))
    add("ufm_link_active_count", {"cluster": c}, sum(lk["active"] for lk in d["links"]))

    # ── Alarms ────────────────────────────────────────────────────────────────
    hdr("Number of active alarms by severity", "gauge", "ufm_alarms_active")
    alarm_by_sev = Counter(a["severity"].upper() for a in d["alarms"])
    for sev, cnt in alarm_by_sev.items():
        add("ufm_alarms_active", {"cluster": c, "severity": sev}, cnt)
    if not alarm_by_sev:
        add("ufm_alarms_active", {"cluster": c, "severity": "NONE"}, 0)

    hdr("Total active alarms", "gauge", "ufm_alarms_total")
    add("ufm_alarms_total", {"cluster": c}, len(d["alarms"]))

    hdr("Number of active alarms by alarm type", "gauge", "ufm_alarms_by_type")
    alarm_by_type = Counter(a["alarm_type"] for a in d["alarms"] if a.get("alarm_type"))
    for atype, cnt in alarm_by_type.items():
        add("ufm_alarms_by_type", {"cluster": c, "alarm_type": atype}, cnt)

    hdr("Number of active alarms by type+description combination",
        "gauge", "ufm_alarms_by_description")
    alarm_by_desc = Counter(
        (a.get("alarm_type",""), a.get("description",""))
        for a in d["alarms"]
    )
    for (atype, desc), cnt in alarm_by_desc.items():
        add("ufm_alarms_by_description",
            {"cluster": c, "alarm_type": atype, "description": desc[:64]}, cnt)

    hdr("Total event_count for alarms grouped by alarm_type (cumulative triggers)",
        "gauge", "ufm_alarms_event_count")
    alarm_event_totals: dict = {}
    for a in d["alarms"]:
        k = (a.get("alarm_type",""), a.get("description",""))
        alarm_event_totals[k] = alarm_event_totals.get(k, 0) + a.get("event_count", 1)
    for (atype, desc), total in alarm_event_totals.items():
        add("ufm_alarms_event_count",
            {"cluster": c, "alarm_type": atype, "description": desc[:64]}, total)

    # ── Events (recent window) ────────────────────────────────────────────────
    hdr("Events seen in last scrape window by type and severity",
        "gauge", "ufm_events_recent")
    ev_by_type_sev = Counter((e["type"], e["severity"].upper()) for e in d["events"])
    for (etype, esev), cnt in ev_by_type_sev.items():
        add("ufm_events_recent", {"cluster": c, "type": etype, "severity": esev}, cnt)

    hdr("Events seen in last scrape window by category", "gauge", "ufm_events_by_category")
    ev_by_cat = Counter(e.get("category", "Unknown") for e in d["events"])
    for cat, cnt in ev_by_cat.items():
        add("ufm_events_by_category", {"cluster": c, "category": cat}, cnt)

    # ── Cluster Minder ────────────────────────────────────────────────────────
    if UFM_CLUSTER_MINDER and d.get("cm"):
        cm = d["cm"]

        hdr("1 if Cluster Minder plugin is responsive", "gauge", "ufm_cm_up")
        add("ufm_cm_up", {"cluster": c}, 1 if cm.get("status") else 0)

        # Per-node Cluster Minder data
        hdr("Cluster Minder node health (1=healthy)", "gauge", "ufm_cm_node_healthy")
        hdr("Cluster Minder node firmware compliance (1=compliant)", "gauge",
            "ufm_cm_node_firmware_compliant")
        hdr("Cluster Minder boot order position (0=first)",
            "gauge", "ufm_cm_boot_order_entry")

        for node in cm.get("nodes", []):
            name   = node.get("name", node.get("node_name", ""))
            guid   = node.get("guid", "")
            health = node.get("health", node.get("status", "")).upper()
            fw_ok  = node.get("firmware_compliant", node.get("fw_compliant", None))

            nlbl = {"cluster": c, "node": name, "guid": guid}
            add("ufm_cm_node_healthy",            nlbl,
                1 if health in ("OK", "HEALTHY", "ACTIVE") else 0)
            if fw_ok is not None:
                add("ufm_cm_node_firmware_compliant", nlbl, 1 if fw_ok else 0)

            for pos, entry in enumerate(node.get("boot_order", [])):
                add("ufm_cm_boot_order_entry",
                    {**nlbl, "entry": str(entry)}, pos)

        # Cluster Minder events
        hdr("Cluster Minder events by severity in last window",
            "gauge", "ufm_cm_events_recent")
        cm_ev_sev = Counter(e.get("severity","").upper() for e in cm.get("events", []))
        for sev, cnt in cm_ev_sev.items():
            add("ufm_cm_events_recent", {"cluster": c, "severity": sev}, cnt)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------
_lock   = threading.Lock()
_latest = ""
_ready  = threading.Event()


def scrape_loop():
    global _latest
    while True:
        log.info("Polling UFM at %s ...", UFM_HOST)
        t0   = time.time()
        data = collect()
        dur  = time.time() - t0
        if data["up"]:
            log.info("UFM up — %d switches, %d ports, %d alarms in %.1fs",
                     len(data["modules"]), len(data["ports"]),
                     len(data["alarms"]), dur)
        else:
            log.warning("UFM unreachable at %s", UFM_HOST)
        metrics = render_metrics(data)
        with _lock:
            _latest = metrics
        _ready.set()
        time.sleep(SCRAPE_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/metrics", "/"):
            _ready.wait(timeout=90)
            with _lock:
                body = _latest.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/-/healthy":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK\n")
        else:
            self.send_response(404)
            self.end_headers()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not UFM_HOST:
        log.error("UFM_HOST not set — set UFM_HOST/UFM_USER/UFM_PASSWORD in your .env")
        sys.exit(1)

    log.info("NVIS UFM Exporter starting")
    log.info("  UFM host        : %s", UFM_HOST)
    log.info("  UFM user        : %s", UFM_USER)
    log.info("  Cluster Minder  : %s", UFM_CLUSTER_MINDER)
    log.info("  Cluster         : %s", CLUSTER_NAME)
    log.info("  Scrape interval : %ds", SCRAPE_INTERVAL)
    log.info("  Listen port     : %d", PORT)

    t = threading.Thread(target=scrape_loop, daemon=True)
    t.start()

    server = HTTPServer(("", PORT), Handler)
    log.info("Metrics available at http://0.0.0.0:%d/metrics", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
