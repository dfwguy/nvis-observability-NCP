#!/usr/bin/env python3
"""
NVIS Redfish BMC Prometheus Exporter
====================================
A single central poller that talks to each node's BMC via the Redfish API and
exposes hardware telemetry as Prometheus metrics. One exporter process scrapes
every BMC in the cluster — nodes never run an agent for this.

Collected per node:
  - System identity (model, serial, UUID, BIOS version, power state)
  - Boot order (detect misconfigured PXE/disk boot sequences)
  - Firmware inventory (NIC / GPU / NVSwitch / drive / BMC / BIOS) + drift vs. baseline
  - Fan speeds (RPM) and health
  - Temperature sensors (Celsius) — CPU, inlet, GPU, NVSwitch, etc.
  - Power draw (watts) — total and per-component (GPU, NVSwitch, NIC, HSC rails)
  - Coolant leak sensors (liquid-cooled trays)
  - System Event Log (SEL) severity counts + newest-entry timestamp

Exposes Prometheus text metrics on $PORT (default 9104). Pure Python stdlib —
no pip dependencies.

Environment variables:
  BMC_TARGETS        Path to a targets file (one BMC per line). Accepted formats:
                       node,ip            e.g.  node001,203.0.113.5
                       ip                 e.g.  203.0.113.5
                       ip/cidr            e.g.  203.0.113.0/28  (expanded)
                     A leading "node,ip" header and blank / '#' lines are ignored.
  BMC_HOSTS          Fallback: inline comma-separated IPs or CIDRs (node = ip).
  BMC_USER           BMC username                              (default: root)
  BMC_PASSWORD       BMC password                              (required)
  BMC_CONCURRENCY    Number of BMCs polled in parallel         (default: 16)
  SENSOR_CONCURRENCY Parallel Redfish requests per BMC         (default: 8)
  CLUSTER_NAME       Label added to every metric               (default: "")
  DGX_FLAVOR         Optional platform hint for firmware drift, e.g. DGXB300.
                     Used only when the BMC does not report a Model string.
  PORT               HTTP listen port                          (default: 9104)
  SCRAPE_INTERVAL    Seconds between full polls                 (default: 60)
  VERIFY_SSL         Verify BMC TLS cert (true/false)          (default: false)
  BMC_TIMEOUT        Per-request timeout seconds               (default: 10)
  FW_DISABLED        Skip firmware inventory (true/false)      (default: false)
  SEL_DISABLED       Skip SEL / event-log collection           (default: false)
  SENSOR_NAME_REGEX  Sensor-basename allow-list regex (see below)
  SEL_LOG_PATH       JSONL file for SEL entries (Loki tail)    (default: /var/log/nvis/redfish-sel.json)

Usage (standalone):
  BMC_TARGETS=/etc/nvis/bmc-targets.csv BMC_USER=root BMC_PASSWORD=secret \
    CLUSTER_NAME=my-cluster python3 redfish_exporter.py

Usage (docker-compose) — see compose.bmc-bcm-netq.yml redfish-bmc-exporter service.
"""

import os
import re
import sys
import time
import json
import calendar
import ipaddress
import threading
import logging
import urllib.request
import urllib.error
import ssl
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("redfish-exporter")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BMC_TARGETS        = os.environ.get("BMC_TARGETS", "")
BMC_HOSTS_RAW      = os.environ.get("BMC_HOSTS", "")
BMC_USER           = os.environ.get("BMC_USER", "root")
# BMC_PASSWORD is the documented name; BMC_PASS is accepted for compatibility.
BMC_PASSWORD       = os.environ.get("BMC_PASSWORD", os.environ.get("BMC_PASS", ""))
BMC_CONCURRENCY    = max(1, int(os.environ.get("BMC_CONCURRENCY", "16")))
SENSOR_CONCURRENCY = max(1, int(os.environ.get("SENSOR_CONCURRENCY", "8")))
CLUSTER_NAME       = os.environ.get("CLUSTER_NAME", "")
DGX_FLAVOR         = os.environ.get("DGX_FLAVOR", "").strip()
PORT               = int(os.environ.get("PORT", "9104"))
SCRAPE_INTERVAL    = int(os.environ.get("SCRAPE_INTERVAL", "60"))
VERIFY_SSL         = os.environ.get("VERIFY_SSL", "false").lower() == "true"
TIMEOUT            = int(os.environ.get("BMC_TIMEOUT", "10"))

# Sensor filter: by default pull EVERY temperature sensor (full historic
# visibility for thermal-spike / random-shutdown correlation) plus fans and key
# power readings. Anything starting TEMP_, or containing _Temp_, Temperature, or
# matching HGX_*, plus SPD_FAN_*, plus power keywords. Override with an empty
# string to keep every sensor, or a tighter regex to shrink scrape time.
SENSOR_NAME_REGEX = os.environ.get(
    "SENSOR_NAME_REGEX",
    r"(?i)("
    r"^TEMP_.*|"                                 # all CPU/baseboard TEMP_* names
    r".*_Temp_[0-9]+$|"                          # HGX_*_Temp_N (NIC, GPU DRAM, etc.)
    r".*_TEMP_[0-9]+$|"                          # HGX_GPU_*_TEMP_N
    r"^SPD_FAN_.*|"                              # all fans
    r".*_Power_[0-9]+$|"                         # power sensors per component
    r"^Total_Power$|^PWR_.*|^AMP_.*_HSC$|"       # PDB total + supply rails
    r".*Temperature.*"                            # generic catch
    r")",
)
_sensor_re = re.compile(SENSOR_NAME_REGEX) if SENSOR_NAME_REGEX else None


def expand_hosts(raw: str) -> list:
    """Expand comma-separated IPs and CIDRs into a flat list of IP strings.

    Non-IP tokens (hostnames) are passed through unchanged.
    """
    hosts = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            net = ipaddress.ip_network(token, strict=False)
            if net.num_addresses == 1:
                hosts.append(str(net.network_address))
            else:
                hosts.extend(str(h) for h in net.hosts())
        except ValueError:
            hosts.append(token)
    return hosts


def load_targets() -> list:
    """Return an ordered list of (node, ip) tuples.

    Priority:
      1. BMC_TARGETS file (one target per line, CSV or whitespace separated).
      2. BMC_HOSTS inline comma-separated IPs / CIDRs (node = ip).
    """
    targets = []
    seen = set()

    def _add(node, ip):
        if ip and ip not in seen:
            seen.add(ip)
            targets.append((node or ip, ip))

    if BMC_TARGETS and os.path.isfile(BMC_TARGETS):
        with open(BMC_TARGETS, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in re.split(r"[,\s]+", line) if p.strip()]
                if not parts:
                    continue
                # Skip a "node,ip" style header row.
                if parts[0].lower() == "node" and len(parts) >= 2 and \
                        parts[1].lower() in ("ip", "bmc", "address", "bmc_ip"):
                    continue
                if len(parts) == 1:
                    ips = expand_hosts(parts[0])
                    for ip in ips:
                        _add(ip, ip)
                else:
                    node = parts[0]
                    ips = expand_hosts(parts[1])
                    if len(ips) == 1:
                        _add(node, ips[0])
                    else:
                        for ip in ips:
                            _add(f"{node}-{ip}", ip)
        return targets

    for ip in expand_hosts(BMC_HOSTS_RAW):
        _add(ip, ip)
    return targets


TARGETS = load_targets()
BMC_HOSTS = [ip for _n, ip in TARGETS]
_NODE_BY_IP = {ip: node for node, ip in TARGETS}


# ---------------------------------------------------------------------------
# DGX firmware baselines — sourced from NVIDIA firmware update guides.
# Keys use substrings of Redfish FirmwareInventory component names so the fuzzy
# matcher in detect_dgx_baseline() can find them without exact-name coupling.
#
# Component name -> Redfish inventory match (substring):
#   HostBIOS     -> HostBIOS_0
#   HostBMC      -> HostBMC_0  (NOT HGX_FW_BMC)
#   HGX_FW_BMC   -> HGX_FW_BMC_0
#   NVSwitch     -> HGX_FW_NVSwitch_*, NVSwitch*
#   ConnectX     -> HGX_FW_ConnectX_*, ConnectX*
#   CX7NIC       -> CX7NIC_*  (host-side NICs)
#   GPU          -> HGX_FW_GPU_*, GPU_SXM_*
# ---------------------------------------------------------------------------
DGX_FIRMWARE_BASELINE = {

    # -- DGX H100 / H200 (same platform, same firmware) -----------------------
    "DGXH100": {
        "26.03.2": {
            "HostBIOS": "1.7.8",    "HostBMC": "26.03.03",
            "HGX_FW_BMC": "HGX-22.10-1-rc83",
            "NVSwitch": "96.10.73.00.01",
            "GPU": "96.00.DA.00.0C",   # H100 VBIOS; H200 = 96.00.DA.00.11
            "NVMe": "GDC7502Q",
        },
        "25.12.2": {
            "HostBIOS": "1.7.8",    "HostBMC": "25.12.11",
            "HGX_FW_BMC": "HGX-22.10-1-rc81",
            "NVSwitch": "96.10.73.00.01",
            "GPU": "96.00.DA.00.0C",
            "NVMe": "GDC7502Q",
        },
        "25.10.1": {
            "HostBIOS": "1.6.7",    "HostBMC": "25.06.27",
            "HGX_FW_BMC": "HGX-22.10-1-rc81",
            "NVSwitch": "96.10.73.00.01",
            "GPU": "96.00.DA.00.0C",
            "NVMe": "GDC7502Q",
        },
        "25.06.4": {
            "HostBIOS": "1.6.7",    "HostBMC": "25.06.27",
            "HGX_FW_BMC": "HGX-22.10-1-rc80",
            "NVSwitch": "96.10.6D.00.01",
            "GPU": "96.00.D0.00.02",
            "NVMe": "GDC7502Q",
        },
        "25.04.3": {
            "HostBIOS": "1.6.7",    "HostBMC": "25.02.12",
            "HGX_FW_BMC": "HGX-22.10-1-rc79",
            "NVSwitch": "96.10.6D.00.01",
            "ConnectX": "28.43.2026",
            "GPU": "96.00.CF.00.01",
            "NVMe": "GDC7502Q",
        },
        "24.09.1": {
            "HostBIOS": "1.05.03",  "HostBMC": "24.09.17",
            "HGX_FW_BMC": "HGX-22.10-1-rc67",
            "NVSwitch": "96.10.57.00.01",
            "ConnectX": "28.39.3560",
            "GPU": "96.00.A5.00.01",
            "NVMe": "GDC7502Q",
        },
    },

    # -- DGX B300 -------------------------------------------------------------
    "DGXB300": {
        "26.03.1": {
            "HostBIOS": "01.00.06", "HostBMC": "01.01.25",
            "HGX_FW_BMC": "B3-2511-04.0",
            "NVSwitch": "35.2014.4770",
            "ConnectX": "40.47.2526",
            "GPU": "97.10.64.00.0C",
        },
        "25.12.1": {
            "HostBIOS": "01.00.05", "HostBMC": "01.00.03",
            "HGX_FW_BMC": "B3-2509-05.0",
            "NVSwitch": "35.2015.4718",
            "GPU": "97.10.52.00.17",
        },
        "25.11.2": {
            "HostBIOS": "01.00.05", "HostBMC": "01.00.03",
            "HGX_FW_BMC": "B3-2509-05.0",
            "NVSwitch": "35.2014.4716",
            "GPU": "97.10.52.00.17",
        },
        "25.11.1": {
            "HostBIOS": "01.00.05", "HostBMC": "01.00.00",
            "HGX_FW_BMC": "B3-2509-05.0",
            "NVSwitch": "35.2014.4716",
            "GPU": "97.10.52.00.17",
        },
    },

    # -- DGX B200 -------------------------------------------------------------
    "DGXB200": {
        "26.03.1": {
            "HostBIOS": "1.7.8",    "HostBMC": "26.03.03",
            "HGX_FW_BMC": "25.05-1-ga35",
            "NVSwitch": "35.2014.4770",
            "GPU": "97.00.E4.00.1E",
            "NVMe": "GDC7502Q",
        },
        "25.12.1": {
            "HostBIOS": "1.7.8",    "HostBMC": "25.12.11",
            "HGX_FW_BMC": "25.05-1-ga35",
            "NVSwitch": "35.2015.4718",
            "GPU": "97.00.D9.00.35",
        },
        "25.09.1": {
            "HostBIOS": "1.6.7",    "HostBMC": "25.06.27",
            "HGX_FW_BMC": "25.02-1-ga19",
            "NVSwitch": "35.2014.4668",
            "GPU": "97.00.C5.00.2D",
        },
        "25.06.3": {
            "HostBIOS": "1.6.7",    "HostBMC": "25.06.27",
            "HGX_FW_BMC": "25.02-1-ga19",
            "NVSwitch": "35.2014.1680",
            "ConnectX": "28.43.2026",
            "GPU": "97.00.9A.00.0F",
        },
        "25.04.1": {
            "HostBIOS": "1.6.7",    "HostBMC": "25.02.12",
            "HGX_FW_BMC": "25.02-1-ga8",
            "NVSwitch": "35.2014.1654",
            "ConnectX": "28.43.2026",
            "GPU": "97.00.88.00.0F",
        },
        "25.02.6": {
            "HostBIOS": "1.6.7",    "HostBMC": "25.02.12",
            "HGX_FW_BMC": "HGX-24.11-1-rc57",
            "NVSwitch": "35.2014.1638",
            "ConnectX": "28.43.2026",
            "GPU": "97.00.7F.00.01",
        },
    },

    # -- DGX A100 (firmware shipped as container; DGX OS versions below) -------
    "DGXA100": {
        "26.2.1": {
            "HostBIOS": "1.32",     "HostBMC": "00.25.02",
            "NVSwitch": "92.10.18.00.01",
            "GPU": "92.00.9E.00.16",
            "NVMe": "GDC7502Q",
        },
        "25.5.4": {
            "HostBIOS": "1.30",     "HostBMC": "00.24.02",
            "NVSwitch": "92.10.18.00.01",
            "GPU": "92.00.9E.00.16",
            "NVMe": "GDC7502Q",
        },
        "24.11.1": {
            "HostBIOS": "1.29",     "HostBMC": "00.24.02",
            "NVSwitch": "92.10.18.00.01",
            "GPU": "92.00.9E.00.16",
        },
        "24.6.1": {
            "HostBIOS": "1.27",     "HostBMC": "00.23.04",
            "NVSwitch": "92.10.18.00.01",
            "GPU": "92.00.9E.00.16",
        },
        "23.12.1": {
            "HostBIOS": "1.25",     "HostBMC": "00.22.05",
            "NVSwitch": "92.10.18.00.01",
            "GPU": "92.00.9E.00.01",
        },
        "23.09.1": {
            "HostBIOS": "1.21",     "HostBMC": "00.21.01",
            "NVSwitch": "92.10.18.00.01",
            "GPU": "92.00.9E.00.01",
        },
        "22.12.1": {
            "HostBIOS": "1.18",     "HostBMC": "00.19.07",
            "NVSwitch": "92.10.18.00.01",
            "GPU": "92.00.9E.00.01",
        },
    },
}

# Reverse map: HostBIOS version -> (platform_key, dgx_os_version) for fast lookup
_BIOS_TO_VERSION = {}
for _plat, _vers in DGX_FIRMWARE_BASELINE.items():
    for _osver, _comps in _vers.items():
        if "HostBIOS" in _comps:
            _BIOS_TO_VERSION[_comps["HostBIOS"]] = (_plat, _osver)


def detect_dgx_baseline(model: str, host_bios: str = ""):
    """Return (dgx_os_version, component_dict) for this model/BIOS combination.

    If host_bios matches a known release exactly, return that release's baseline.
    Otherwise return the latest known release for the detected platform.
    Falls back to the DGX_FLAVOR hint when the BMC does not report a Model.
    Returns ("", {}) when the model is not recognised.
    """
    model_upper = (model or DGX_FLAVOR).upper()
    matched_platform = None
    for pattern in DGX_FIRMWARE_BASELINE:
        if pattern.upper() in model_upper:
            matched_platform = pattern
            break
    if not matched_platform:
        return "", {}

    versions = DGX_FIRMWARE_BASELINE[matched_platform]

    # Exact BIOS match -> identify the specific DGX OS release
    if host_bios:
        for os_ver, comps in versions.items():
            if comps.get("HostBIOS") == host_bios:
                return os_ver, comps

    # Fall back: latest release for this platform
    latest = sorted(versions.keys(), reverse=True)[0]
    return latest, versions[latest]


# ---------------------------------------------------------------------------
# Redfish HTTP helper
# ---------------------------------------------------------------------------
def redfish_get(bmc_ip: str, path: str):
    url = f"https://{bmc_ip}{path}"
    ctx = ssl.create_default_context()
    if not VERIFY_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    token = base64.b64encode(f"{BMC_USER}:{BMC_PASSWORD}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "OData-Version": "4.0",
    })
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code not in (404, 501):
            log.debug("%s %s -> HTTP %d", bmc_ip, path, e.code)
        return None
    except Exception as e:
        log.debug("%s %s -> %s", bmc_ip, path, e)
        return None


# ---------------------------------------------------------------------------
# Per-BMC collector
# ---------------------------------------------------------------------------
def collect_bmc(bmc_ip: str) -> dict:
    data = {
        "ip":            bmc_ip,
        "node":          _NODE_BY_IP.get(bmc_ip, ""),
        "reachable":     False,
        "model":         "",
        "manufacturer":  "",
        "serial":        "",
        "uuid":          "",
        "bios_version":  "",
        "boot_order":    [],
        "power_state":   "",
        "power_watts":   None,
        "fans":          [],   # [{name, rpm, status}]
        "temps":         [],   # [{name, celsius, status}]
        "firmware":      [],   # [{name, version, component_type}]
        "leak_sensors":  [],   # [{name, state, value}]  0=OK 1=Leak
        "power_sensors": [],   # [{name, watts}] — per-component (GPU, NVSwitch, NIC, HSC)
        "sel_entries":   [],   # [{id, ts, severity, message, message_id, sensor_type}]
        "sel_severity_counts": dict(_SEL_STATE.get(bmc_ip, {}).get("severity_counts",
                                                                  {"OK": 0, "Warning": 0, "Critical": 0})),
        "sel_latest_ts": _SEL_STATE.get(bmc_ip, {}).get("latest_ts"),
    }

    # -- System root ----------------------------------------------------------
    sys_root = redfish_get(bmc_ip, "/redfish/v1/Systems")
    if not sys_root:
        return data

    # Find first system member
    members = sys_root.get("Members", [])
    if not members:
        return data
    sys_path = members[0].get("@odata.id", "/redfish/v1/Systems/1")

    system = redfish_get(bmc_ip, sys_path)
    if not system:
        return data

    data["reachable"]    = True
    data["model"]        = system.get("Model", "")
    data["manufacturer"] = system.get("Manufacturer", "")
    data["serial"]       = system.get("SerialNumber", "")
    data["uuid"]         = system.get("UUID", "")
    data["power_state"]  = system.get("PowerState", "")

    # BIOS version
    bios_ver = system.get("BiosVersion", "")
    data["bios_version"] = bios_ver

    # Boot order
    boot = system.get("Boot", {})
    # Try BootOrder array first (Redfish 1.5+), fall back to BootSourceOverrideTarget
    boot_order = boot.get("BootOrder", [])
    if not boot_order:
        override = boot.get("BootSourceOverrideTarget", "")
        if override:
            boot_order = [override]
    data["boot_order"] = boot_order

    # -- Bios sub-resource (detailed BIOS settings) ---------------------------
    bios_link = system.get("Bios", {}).get("@odata.id", "")
    if bios_link and not bios_ver:
        bios_data = redfish_get(bmc_ip, bios_link)
        if bios_data:
            data["bios_version"] = bios_data.get("Attributes", {}).get(
                "SystemBiosVersion", bios_data.get("Version", "")
            )

    # -- FAST PATH: legacy /Thermal at /Chassis/DGX returns all temps + fans in
    # a single call on modern DGX/HGX BMCs. Always try this FIRST — if it
    # succeeds, we skip the expensive multi-chassis per-sensor walk entirely.
    fast_thermal = redfish_get(bmc_ip, "/redfish/v1/Chassis/DGX/Thermal")
    fast_path_ok = False
    if fast_thermal:
        for fan in fast_thermal.get("Fans", []):
            name    = fan.get("Name", fan.get("FanName", ""))
            rpm     = fan.get("Reading", fan.get("CurrentReading"))
            status  = fan.get("Status", {}).get("Health", "")
            if name and rpm is not None:
                data["fans"].append({"name": name, "rpm": rpm, "status": status})
        for sensor in fast_thermal.get("Temperatures", []):
            name     = sensor.get("Name", "")
            celsius  = sensor.get("ReadingCelsius")
            status   = sensor.get("Status", {}).get("Health", "")
            if name and celsius is not None:
                data["temps"].append({"name": name, "celsius": celsius, "status": status})
        fast_path_ok = bool(data["temps"]) or bool(data["fans"])

    # -- Per-component power via TelemetryService MetricReport (fast path) -----
    # ONE call returns many power values: per-GPU, per-GPU-DRAM, per-NVSwitch,
    # per-NIC, HSC/HSCC rails, TotalGPU, TotalHSC.
    tel = redfish_get(bmc_ip,
        "/redfish/v1/TelemetryService/MetricReports/HGX_PlatformEnvironmentMetrics_0")
    if tel:
        for mv in tel.get("MetricValues", []):
            mp = mv.get("MetricProperty", "")
            val = mv.get("MetricValue", "")
            if not mp or not val:
                continue
            comp = mp.rstrip("/").rsplit("/", 1)[-1]
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            # Classify by path token (case-insensitive)
            mp_lower = mp.lower()
            if "power" in mp_lower:
                data["power_sensors"].append({"name": comp, "watts": num})
            elif "temp" in mp_lower:
                # Per-component temps: GPU, NVSwitch, NIC, HSC, etc.
                if not any(t["name"] == comp for t in data["temps"]):
                    data["temps"].append({"name": comp, "celsius": num, "status": "OK"})

    fast_power = redfish_get(bmc_ip, "/redfish/v1/Chassis/DGX/Power")
    if fast_power:
        for ctrl in fast_power.get("PowerControl", []):
            # Newer firmware: PowerControl[].PowerMetrics.AverageConsumedWatts
            # Legacy firmware: PowerControl[].PowerConsumedWatts
            consumed = (ctrl.get("PowerConsumedWatts")
                        or ctrl.get("PowerMetrics", {}).get("AverageConsumedWatts"))
            if consumed is not None and data["power_watts"] is None:
                data["power_watts"] = consumed
                # Also capture max/min for dashboard ranges
                pm = ctrl.get("PowerMetrics", {}) or {}
                if pm.get("MaxConsumedWatts") is not None:
                    data["power_max_watts"] = pm["MaxConsumedWatts"]
                if pm.get("MinConsumedWatts") is not None:
                    data["power_min_watts"] = pm["MinConsumedWatts"]
                break

    # If the fast path produced data, skip the slow chassis-walk entirely.
    chassis_paths = []
    cached = _CHASSIS_CACHE.get(bmc_ip)
    now = time.time()
    if fast_path_ok:
        chassis_paths = []  # skip
    elif cached and now - cached[0] < _CHASSIS_TTL:
        chassis_paths = cached[1]
    else:
        next_path = "/redfish/v1/Chassis"
        for _hop in range(10):
            page = redfish_get(bmc_ip, next_path)
            if not page:
                break
            for m in page.get("Members", []):
                p = m.get("@odata.id", "")
                if p:
                    chassis_paths.append(p)
            nl = page.get("Members@odata.nextLink", "")
            if not nl:
                break
            next_path = nl
        # Filter out chassis with no useful telemetry (EROT/IROT attestation,
        # bridges, slots). Walking these adds latency for zero data.
        skip_re = re.compile(r"(?i)(erot|irot|bridge|slot|connector|attestation)")
        chassis_paths = [c for c in chassis_paths if not skip_re.search(c)]
        _CHASSIS_CACHE[bmc_ip] = (now, chassis_paths)

    # Primary chassis (first one) — kept for legacy fields like power_watts and
    # leak detection that some BMCs only expose at the top-level chassis.
    chassis_path = chassis_paths[0] if chassis_paths else ""

    # -- Sensors collection (Redfish 2023.3+) ---------------------------------
    # Newer BMCs return 404 on /Thermal and /Power; all telemetry lives under
    # {chassis}/Sensors with ReadingType in {Temperature, Rotational, Power}.
    # Large systems expose many chassis, so we parallelize both the /Sensors
    # index walk AND the per-sensor body fetch.
    sensor_paths = []

    def _fetch_sensor_collection(ch_path: str):
        coll = redfish_get(bmc_ip, f"{ch_path.rstrip('/')}/Sensors")
        if not coll:
            return []
        return [sm.get("@odata.id", "")
                for sm in coll.get("Members", []) if sm.get("@odata.id")]

    # BMC HTTP servers (esp. AMI MegaRAC, AspeedBMC) typically cap concurrent
    # client connections at 4-8. Going above that yields connection resets and
    # silently-empty responses — keep SENSOR_CONCURRENCY conservative.
    with ThreadPoolExecutor(max_workers=SENSOR_CONCURRENCY) as ex:
        for paths in ex.map(_fetch_sensor_collection, chassis_paths):
            sensor_paths.extend(paths)

    # Filter by sensor basename regex BEFORE fetching bodies. The Redfish
    # @odata.id includes the sensor Id (e.g. .../Sensors/TEMP_CPU0), so we can
    # narrow hundreds of paths to dozens with no extra HTTP calls.
    if _sensor_re:
        sensor_paths = [p for p in sensor_paths
                        if _sensor_re.match(p.rstrip("/").rsplit("/", 1)[-1])]

    if sensor_paths:
        seen_names = set()
        def _fetch(p):
            return p, redfish_get(bmc_ip, p)
        # Same connection-cap reason as the chassis walk above.
        with ThreadPoolExecutor(max_workers=SENSOR_CONCURRENCY) as ex:
            for _sp, body in ex.map(_fetch, sensor_paths):
                if not body:
                    continue
                rt      = body.get("ReadingType", "")
                name    = body.get("Name") or body.get("Id") or ""
                reading = body.get("Reading")
                status  = body.get("Status", {}).get("Health", "")
                if reading is None or not name:
                    continue
                if name in seen_names:
                    continue
                seen_names.add(name)
                if rt == "Temperature":
                    data["temps"].append({"name": name, "celsius": reading, "status": status})
                elif rt == "Rotational":
                    data["fans"].append({"name": name, "rpm": reading, "status": status})
                elif rt == "Power":
                    # Aggregate total system power from named "Total" sensors
                    if data["power_watts"] is None and "total" in name.lower():
                        data["power_watts"] = reading

    # -- Legacy /Thermal and /Power (older BMCs) ------------------------------
    # Falls back when the Sensors collection is empty (older Redfish < 2023.3).
    if chassis_path:
        thermal = redfish_get(bmc_ip, f"{chassis_path}/Thermal")
        if thermal:
            for fan in thermal.get("Fans", []):
                name    = fan.get("Name", fan.get("FanName", ""))
                rpm     = fan.get("Reading", fan.get("CurrentReading"))
                status  = fan.get("Status", {}).get("Health", "")
                if name and not any(f["name"] == name for f in data["fans"]):
                    data["fans"].append({"name": name, "rpm": rpm, "status": status})
            for sensor in thermal.get("Temperatures", []):
                name     = sensor.get("Name", "")
                celsius  = sensor.get("ReadingCelsius")
                status   = sensor.get("Status", {}).get("Health", "")
                if name and celsius is not None and not any(t["name"] == name for t in data["temps"]):
                    data["temps"].append({"name": name, "celsius": celsius, "status": status})

        power_data = redfish_get(bmc_ip, f"{chassis_path}/Power")
        if power_data:
            for ctrl in power_data.get("PowerControl", []):
                consumed = ctrl.get("PowerConsumedWatts")
                if consumed is not None and data["power_watts"] is None:
                    data["power_watts"] = consumed
                    break

    # -- Firmware inventory ---------------------------------------------------
    # FW_DISABLED=true skips firmware fetch (many components x seconds each on a
    # large system). Disabled by default for the standalone fast-path stack.
    fw_inv = None if os.environ.get("FW_DISABLED", "false").lower() == "true" \
        else redfish_get(bmc_ip, "/redfish/v1/UpdateService/FirmwareInventory")
    if fw_inv:
        for member in fw_inv.get("Members", []):
            fw_path = member.get("@odata.id", "")
            if not fw_path:
                continue
            fw_item = redfish_get(bmc_ip, fw_path)
            if not fw_item:
                continue
            name    = fw_item.get("Name", fw_item.get("Id", ""))
            version = fw_item.get("Version", "")
            ftype   = fw_item.get("SoftwareId", fw_item.get("Description", ""))
            if name and version:
                data["firmware"].append({
                    "name":    name,
                    "version": version,
                    "type":    ftype,
                })

    # -- Leak detection (liquid-cooled tray-level via BMC Redfish) ------------
    # Standard Redfish 2023.3+: LeakDetection resource under Chassis.
    # Liquid-cooled DGX/NVL racks: cold-plate and inner-manifold sensors per
    # compute tray. DetectorState: "OK" (no leak) | "Leak" | "Unplugged".
    if chassis_path:
        leak = redfish_get(bmc_ip, f"{chassis_path}/LeakDetection")
        if leak:
            # Standard schema: Detectors array directly on LeakDetection
            detectors = leak.get("Detectors", [])
            # Older OEM schema: DetectorGroups[].Detectors[]
            for grp in leak.get("DetectorGroups", []):
                detectors.extend(grp.get("Detectors", []))
            for d in detectors:
                sensor_name = d.get("Name", d.get("PhysicalContext", "unknown"))
                state       = d.get("DetectorState", d.get("Status", {}).get("State", "Unknown"))
                data["leak_sensors"].append({
                    "name":  sensor_name,
                    "state": state,   # "OK", "Leak", "Unplugged", "Unknown"
                    "value": 1 if str(state).lower() == "leak" else 0,
                })

    # -- System Event Log (SEL) / EventLog / AuditLog -------------------------
    # Redfish exposes event logs under two roots:
    #   - {System}/LogServices/*    (boot/BIOS-side events)
    #   - {Manager}/LogServices/*   (BMC SEL, HGX-side EventLog, AuditLog)
    # Service Ids worth scraping: SEL, EventLog, AuditLog, and the
    # /Chassis/DGX/LogServices/Logs endpoint (power-rail SEL events not exposed
    # elsewhere on some firmware). Excludes Dump (binary blobs) and DebugToken.
    sel_re = re.compile(r"^(sel|eventlog|auditlog|logs)$", re.IGNORECASE)
    log_roots = []
    if os.environ.get("SEL_DISABLED", "false").lower() != "true":
        log_roots.append(sys_path)
        mgr_root = redfish_get(bmc_ip, "/redfish/v1/Managers")
        if mgr_root:
            for mm in mgr_root.get("Members", []):
                mp = mm.get("@odata.id", "")
                if mp:
                    log_roots.append(mp)
        # /Chassis/DGX has its own LogServices on some firmware — power-rail SEL.
        log_roots.append("/redfish/v1/Chassis/DGX")

    for root_path in log_roots:
        log_svcs = redfish_get(bmc_ip, f"{root_path.rstrip('/')}/LogServices")
        if not log_svcs:
            continue
        for svc_member in log_svcs.get("Members", []):
            svc_path = svc_member.get("@odata.id", "")
            if not svc_path:
                continue
            svc_id = svc_path.rstrip("/").rsplit("/", 1)[-1]
            if not sel_re.match(svc_id):
                continue
            # Incremental fetch: probe count cheaply, skip if unchanged, otherwise
            # only fetch entries past the previous high-water-mark.
            base_entries = f"{svc_path.rstrip('/')}/Entries"
            peek = redfish_get(bmc_ip, f"{base_entries}?$top=0") or \
                   redfish_get(bmc_ip, f"{base_entries}?$top=1")
            cur_count = (peek or {}).get("Members@odata.count", 0) if peek else 0
            hw_key = (bmc_ip, svc_path)
            prev_count = _SEL_COUNT_HW.get(hw_key, 0)
            if cur_count and cur_count == prev_count:
                # No new entries — accounting stays at the prior numbers.
                continue
            skip = prev_count if cur_count > prev_count else 0
            # $top=6000 returns all entries in ONE call — faster than paginating.
            top_param = "$top=6000"
            if skip:
                entries_path = f"{base_entries}?$skip={skip}&{top_param}"
            else:
                entries_path = f"{base_entries}?{top_param}"
            collected = 0
            for _hop in range(5):
                page = redfish_get(bmc_ip, entries_path)
                if not page:
                    break
                for entry in page.get("Members", []):
                    if collected >= 500:
                        break
                    eid       = entry.get("Id", "")
                    # Prefer EventTimestamp when present (real wall-clock time);
                    # Created on some BMCs is fixed at firmware build time.
                    created   = entry.get("EventTimestamp") or entry.get("Created", "")
                    severity  = entry.get("Severity", "")
                    message   = entry.get("Message", "")
                    msg_id    = entry.get("MessageId", "")
                    sensor_t  = entry.get("SensorType", "")
                    ent_type  = entry.get("EntryType", "")
                    ts_unix   = None
                    if created:
                        try:
                            iso = created.replace("Z", "+00:00")
                            ts_unix = calendar.timegm(
                                datetime.fromisoformat(iso).utctimetuple()
                            )
                        except (ValueError, TypeError):
                            ts_unix = None
                    data["sel_entries"].append({
                        "id":          f"{svc_id}/{eid}",
                        "source":      svc_id,
                        "ts":          created,
                        "ts_unix":     ts_unix,
                        "severity":    severity,
                        "message":     message,
                        "message_id":  msg_id,
                        "sensor_type": sensor_t,
                        "entry_type":  ent_type,
                    })
                    sev_key = severity if severity in ("OK", "Warning", "Critical") else "OK"
                    data["sel_severity_counts"][sev_key] = (
                        data["sel_severity_counts"].get(sev_key, 0) + 1
                    )
                    if ts_unix is not None and (
                        data["sel_latest_ts"] is None or ts_unix > data["sel_latest_ts"]
                    ):
                        data["sel_latest_ts"] = ts_unix
                    collected += 1
                if collected >= 500:
                    break
                next_link = page.get("Members@odata.nextLink", "")
                if not next_link:
                    break
                entries_path = next_link
            # Save high-water-mark so next scrape can incrementally fetch.
            if cur_count:
                _SEL_COUNT_HW[hw_key] = cur_count

    # Persist cumulative SEL state for the next scrape's starting point.
    _SEL_STATE[bmc_ip] = {
        "severity_counts": dict(data["sel_severity_counts"]),
        "latest_ts": data["sel_latest_ts"],
    }
    return data


# ---------------------------------------------------------------------------
# Metrics renderer
# ---------------------------------------------------------------------------
def esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _node_of(r: dict) -> str:
    """Friendly node label: targets-file node -> serial -> ip."""
    return r.get("node") or r.get("serial") or r["ip"]


def render_metrics(results: list) -> str:
    cluster = CLUSTER_NAME
    lines = []

    def add(metric, labels_dict, value):
        lstr = ",".join(f'{k}="{esc(v)}"' for k, v in labels_dict.items())
        lines.append(f"{metric}{{{lstr}}} {value}")

    # -- redfish_up -----------------------------------------------------------
    lines.append("# HELP redfish_up 1 if BMC Redfish API is reachable")
    lines.append("# TYPE redfish_up gauge")
    for r in results:
        add("redfish_up", {"bmc": r["ip"], "cluster": cluster,
                           "node": _node_of(r)}, 1 if r["reachable"] else 0)

    # -- System info (info metric — always 1, data in labels) -----------------
    lines.append("\n# HELP redfish_system_info System identity — model, serial, BIOS version")
    lines.append("# TYPE redfish_system_info gauge")
    for r in results:
        if not r["reachable"]:
            continue
        add("redfish_system_info", {
            "bmc":          r["ip"],
            "cluster":      cluster,
            "node":         _node_of(r),
            "model":        r["model"],
            "manufacturer": r["manufacturer"],
            "serial":       r["serial"],
            "uuid":         r["uuid"],
            "bios_version": r["bios_version"],
            "power_state":  r["power_state"],
        }, 1)

    # -- Boot order -----------------------------------------------------------
    lines.append("\n# HELP redfish_boot_order_entry Boot order position (0=first). "
                 "Use to detect misconfigured PXE/disk boot sequences.")
    lines.append("# TYPE redfish_boot_order_entry gauge")
    for r in results:
        if not r["reachable"]:
            continue
        for pos, entry in enumerate(r["boot_order"]):
            add("redfish_boot_order_entry", {
                "bmc":     r["ip"],
                "cluster": cluster,
                "node":    _node_of(r),
                "serial":  r["serial"],
                "entry":   str(entry),
            }, pos)

    # -- Firmware versions ----------------------------------------------------
    lines.append("\n# HELP redfish_firmware_info Firmware version per component "
                 "(NIC, GPU, drives, BMC, BIOS). Value is always 1 — version in label.")
    lines.append("# TYPE redfish_firmware_info gauge")
    for r in results:
        if not r["reachable"]:
            continue
        for fw in r["firmware"]:
            add("redfish_firmware_info", {
                "bmc":       r["ip"],
                "cluster":   cluster,
                "node":      _node_of(r),
                "serial":    r["serial"],
                "component": fw["name"],
                "version":   fw["version"],
                "type":      fw["type"],
            }, 1)

    # -- Fan speeds -----------------------------------------------------------
    lines.append("\n# HELP redfish_fan_rpm Fan speed in RPM")
    lines.append("# TYPE redfish_fan_rpm gauge")
    lines.append("# HELP redfish_fan_health 1 if fan health is OK")
    lines.append("# TYPE redfish_fan_health gauge")
    for r in results:
        if not r["reachable"]:
            continue
        for fan in r["fans"]:
            if fan["rpm"] is not None:
                add("redfish_fan_rpm", {
                    "bmc": r["ip"], "cluster": cluster, "node": _node_of(r),
                    "serial": r["serial"], "fan": fan["name"],
                }, fan["rpm"])
            add("redfish_fan_health", {
                "bmc": r["ip"], "cluster": cluster, "node": _node_of(r),
                "serial": r["serial"], "fan": fan["name"],
                "status": fan["status"],
            }, 1 if fan["status"].upper() == "OK" else 0)

    # -- Temperatures ---------------------------------------------------------
    lines.append("\n# HELP redfish_temperature_celsius Temperature sensor reading in Celsius")
    lines.append("# TYPE redfish_temperature_celsius gauge")
    for r in results:
        if not r["reachable"]:
            continue
        for temp in r["temps"]:
            add("redfish_temperature_celsius", {
                "bmc": r["ip"], "cluster": cluster, "node": _node_of(r),
                "serial": r["serial"], "sensor": temp["name"],
                "status": temp["status"],
            }, temp["celsius"])

    # -- Power ----------------------------------------------------------------
    lines.append("\n# HELP redfish_power_watts Current power consumption in watts")
    lines.append("# TYPE redfish_power_watts gauge")
    for r in results:
        if not r["reachable"] or r["power_watts"] is None:
            continue
        add("redfish_power_watts", {
            "bmc": r["ip"], "cluster": cluster, "node": _node_of(r),
            "serial": r["serial"],
        }, r["power_watts"])

    # -- Per-component power (TelemetryService HGX_PlatformEnvironmentMetrics) -
    lines.append("\n# HELP redfish_component_power_watts Per-component power (GPU, NVSwitch, NIC, HSC rails)")
    lines.append("# TYPE redfish_component_power_watts gauge")
    for r in results:
        if not r["reachable"]:
            continue
        for ps in r.get("power_sensors", []):
            add("redfish_component_power_watts", {
                "bmc": r["ip"], "cluster": cluster, "node": _node_of(r),
                "serial": r["serial"], "component": ps["name"],
            }, ps["watts"])

    # -- Leak detection (tray-level: cold plate + inner manifold) -------------
    # Liquid-cooled rack BMCs expose per-tray leak sensors via Redfish
    # LeakDetection. Value: 0 = OK (no leak), 1 = Leak detected.
    # Rack/CDU-level isolation status requires NMC + BMS integration.
    lines.append("\n# HELP rf_leakdetector_state Tray-level leak sensor state (0=OK 1=Leak)")
    lines.append("# TYPE rf_leakdetector_state gauge")
    for r in results:
        if not r["reachable"]:
            continue
        for sensor in r.get("leak_sensors", []):
            # Normalize sensor name to lowercase, spaces/dashes -> underscores
            sname = sensor["name"].lower().replace(" ", "_").replace("-", "_")
            add("rf_leakdetector_state", {
                "bmc":    r["ip"],
                "cluster": cluster,
                "node":   _node_of(r),
                "serial": r["serial"],
                "sensor": sname,                  # e.g. "coldplate", "inner_manifold"
                "state":  sensor["state"],         # "OK", "Leak", "Unplugged"
            }, sensor["value"])

    # Convenience aliases for legacy leak-panel metric names (dashboard back-compat).
    lines.append("\n# HELP rf_chassis_0_leakdetector_0_coldplate Cold plate leak (0=OK 1=Leak)")
    lines.append("# TYPE rf_chassis_0_leakdetector_0_coldplate gauge")
    lines.append("# HELP rf_chassis_0_leakdetector_0_manifold Manifold leak (0=OK 1=Leak)")
    lines.append("# TYPE rf_chassis_0_leakdetector_0_manifold gauge")
    for r in results:
        if not r["reachable"]:
            continue
        cp_val  = next((s["value"] for s in r.get("leak_sensors", [])
                        if "cold" in s["name"].lower() or "plate" in s["name"].lower()), 0)
        mfld_val = next((s["value"] for s in r.get("leak_sensors", [])
                         if "manifold" in s["name"].lower()), 0)
        base = {"bmc": r["ip"], "cluster": cluster, "node": _node_of(r), "serial": r["serial"]}
        add("rf_chassis_0_leakdetector_0_coldplate", base, cp_val)
        add("rf_chassis_0_leakdetector_0_manifold",  base, mfld_val)

    # -- Firmware drift vs. DGX baseline --------------------------------------
    lines.append("\n# HELP redfish_firmware_drift 1=firmware differs from DGX baseline, 0=matches, -1=no baseline")
    lines.append("# TYPE redfish_firmware_drift gauge")
    lines.append("\n# HELP redfish_firmware_baseline_version Expected firmware version from NVIDIA DGX baseline (value=1, version in label)")
    lines.append("# TYPE redfish_firmware_baseline_version gauge")
    for r in results:
        if not r["reachable"]:
            continue
        dgx_os_ver, baseline = detect_dgx_baseline(r["model"], r.get("bios_version", ""))
        dgx_model = r["model"] or DGX_FLAVOR
        serial = r["serial"]
        for component, expected_ver in baseline.items():
            add("redfish_firmware_baseline_version", {
                "cluster": cluster, "node": _node_of(r), "serial": serial,
                "dgx_model": dgx_model, "dgx_os_version": dgx_os_ver,
                "component": component, "expected_version": expected_ver,
            }, 1)
        for fw in r["firmware"]:
            component = fw["name"]
            actual_version = fw["version"]
            if not baseline:
                drift_value, expected_ver = -1, ""
            else:
                expected_ver = baseline.get(component, "")
                if not expected_ver:
                    for bkey, bver in baseline.items():
                        if bkey.lower() in component.lower() or component.lower() in bkey.lower():
                            expected_ver = bver
                            break
                drift_value = -1 if not expected_ver else (0 if actual_version == expected_ver else 1)
            add("redfish_firmware_drift", {
                "cluster": cluster, "node": _node_of(r), "serial": serial,
                "component": component, "actual_version": actual_version,
                "expected_version": expected_ver,
                "dgx_model": dgx_model, "dgx_os_version": dgx_os_ver,
            }, drift_value)

    # -- SEL severity counts and latest timestamp -----------------------------
    lines.append("\n# HELP redfish_sel_severity_count Count of SEL entries per severity at last scrape")
    lines.append("# TYPE redfish_sel_severity_count gauge")
    lines.append("# HELP redfish_sel_latest_timestamp Unix timestamp of most recent SEL entry")
    lines.append("# TYPE redfish_sel_latest_timestamp gauge")
    for r in results:
        if not r["reachable"]:
            continue
        base = {"bmc": r["ip"], "cluster": cluster, "node": _node_of(r), "serial": r["serial"]}
        for sev in ("OK", "Warning", "Critical"):
            add("redfish_sel_severity_count",
                {**base, "severity": sev},
                r.get("sel_severity_counts", {}).get(sev, 0))
        if r.get("sel_latest_ts") is not None:
            add("redfish_sel_latest_timestamp", base, r["sel_latest_ts"])

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SEL -> Loki JSON log file
# ---------------------------------------------------------------------------
_SEL_LOG_PATH = os.environ.get("SEL_LOG_PATH", "/var/log/nvis/redfish-sel.json")
_SEL_SEEN = set()
_SEL_SEEN_CAP = 50000

# Per-BMC chassis topology cache and SEL high-water-mark cache. Both reset on
# process restart. Topology TTL is long; SEL HW marks store the last seen
# `Members@odata.count` per (bmc,svc_path) so we skip refetching when stable.
_CHASSIS_CACHE = {}
_CHASSIS_TTL = int(os.environ.get("CHASSIS_TTL", "1800"))   # 30 min
_SEL_COUNT_HW = {}
# Cumulative SEL state per BMC — survives across scrapes so the metric values
# stay populated even when an incremental scrape adds zero new entries.
_SEL_STATE = {}


def write_sel_logfile(results: list) -> None:
    """Append NEW SEL entries (not seen in prior scrapes) as JSON lines.

    A log collector (Alloy/Promtail) tails this file with job="redfish-sel"
    and ships it to Loki.
    """
    new_lines = []
    for r in results:
        if not r.get("reachable"):
            continue
        for entry in r.get("sel_entries", []):
            key = (r["ip"], str(entry.get("id", "")))
            if key in _SEL_SEEN:
                continue
            _SEL_SEEN.add(key)
            new_lines.append(json.dumps({
                "ts":          entry.get("ts", ""),
                "ts_unix":     entry.get("ts_unix"),
                "bmc":         r["ip"],
                "node":        _node_of(r),
                "serial":      r.get("serial", ""),
                "cluster":     CLUSTER_NAME,
                "id":          entry.get("id", ""),
                "source":      entry.get("source", ""),
                "severity":    entry.get("severity", ""),
                "message":     entry.get("message", ""),
                "message_id":  entry.get("message_id", ""),
                "sensor_type": entry.get("sensor_type", ""),
                "entry_type":  entry.get("entry_type", ""),
            }, separators=(",", ":")))

    if not new_lines:
        return

    try:
        os.makedirs(os.path.dirname(_SEL_LOG_PATH), exist_ok=True)
        with open(_SEL_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n".join(new_lines) + "\n")
    except OSError as e:
        log.warning("Failed writing SEL log %s: %s", _SEL_LOG_PATH, e)

    if len(_SEL_SEEN) > _SEL_SEEN_CAP:
        # Trim oldest half by rebuilding from the tail.
        keep = list(_SEL_SEEN)[-_SEL_SEEN_CAP // 2:]
        _SEL_SEEN.clear()
        _SEL_SEEN.update(keep)


# ---------------------------------------------------------------------------
# Background scrape loop — polls all BMCs with bounded concurrency
# ---------------------------------------------------------------------------
_lock          = threading.Lock()
_latest        = ""
_ready         = threading.Event()
_force_refresh = threading.Event()
_refresh_targets = []  # if non-empty, next forced scrape targets only these BMCs


def scrape_all(hosts=None) -> list:
    hosts = list(BMC_HOSTS) if hosts is None else list(hosts)
    if not hosts:
        return []
    results_by_ip = {}
    workers = min(BMC_CONCURRENCY, len(hosts))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_ip = {ex.submit(collect_bmc, ip): ip for ip in hosts}
        for fut in as_completed(future_to_ip):
            ip = future_to_ip[fut]
            try:
                results_by_ip[ip] = fut.result()
            except Exception as exc:  # noqa: BLE001 — one bad BMC must not sink the scrape
                log.warning("collect_bmc(%s) crashed: %s", ip, exc)
                results_by_ip[ip] = {
                    "ip": ip, "node": _NODE_BY_IP.get(ip, ""),
                    "reachable": False, "serial": "",
                }
    # Preserve the target ordering.
    return [results_by_ip[ip] for ip in hosts if ip in results_by_ip]


def scrape_loop():
    global _latest
    last_results = []
    while True:
        with _lock:
            targets = list(_refresh_targets)
            _refresh_targets.clear()
        if targets and last_results:
            log.info("Polling %d BMC host(s) (targeted: %s)...", len(targets), ",".join(targets))
            t0 = time.time()
            partial = scrape_all(targets)
            dur = time.time() - t0
            by_ip = {r["ip"]: r for r in last_results}
            for r in partial:
                by_ip[r["ip"]] = r
            results = [by_ip[ip] for ip in BMC_HOSTS if ip in by_ip]
            up = sum(1 for r in partial if r["reachable"])
            log.info("Done — %d/%d reachable in %.1fs (targeted)", up, len(partial), dur)
        else:
            log.info("Polling %d BMC host(s) (concurrency=%d)...", len(BMC_HOSTS), BMC_CONCURRENCY)
            t0 = time.time()
            results = scrape_all()
            up   = sum(1 for r in results if r["reachable"])
            dur  = time.time() - t0
            log.info("Done — %d/%d reachable in %.1fs", up, len(results), dur)
        last_results = results
        metrics = render_metrics(results)
        with _lock:
            _latest = metrics
        _ready.set()
        write_sel_logfile(results)
        triggered = _force_refresh.wait(timeout=SCRAPE_INTERVAL)
        if triggered:
            _force_refresh.clear()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _handle_refresh(self):
        # Parse optional ?bmc=<ip> query parameter without adding new imports.
        path, _, query = self.path.partition("?")
        if path != "/refresh":
            self.send_response(404)
            self.end_headers()
            return
        bmc_param = None
        for part in query.split("&"):
            if not part:
                continue
            k, _, v = part.partition("=")
            if k == "bmc":
                bmc_param = v
                break
        if bmc_param:
            if bmc_param not in BMC_HOSTS:
                err = (b'{"status": "error", "error": "bmc not in targets", '
                       b'"bmc": "' + bmc_param.encode() + b'"}\n')
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            with _lock:
                if bmc_param not in _refresh_targets:
                    _refresh_targets.append(bmc_param)
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log.info("[DRIFT-CHECK] Manual refresh triggered at %s (bmc=%s)", ts, bmc_param)
            _force_refresh.set()
            body = (b'{"status": "refresh triggered", "bmc": "'
                    + bmc_param.encode() + b'"}\n')
        else:
            with _lock:
                _refresh_targets.clear()
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log.info("[DRIFT-CHECK] Manual refresh triggered at %s", ts)
            _force_refresh.set()
            body = b'{"status": "refresh triggered", "bmc": "all"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/metrics", "/"):
            _ready.wait(timeout=120)
            with _lock:
                body = _latest.encode()
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/-/healthy", "/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK\n")
        elif path == "/refresh":
            self._handle_refresh()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/refresh":
            self._handle_refresh()
        else:
            self.send_response(404)
            self.end_headers()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not BMC_HOSTS:
        log.error("No BMC targets — set BMC_TARGETS (file) or BMC_HOSTS (inline)")
        sys.exit(1)
    if not BMC_PASSWORD:
        log.error("BMC_PASSWORD is empty — set BMC_PASSWORD (BMC user credential)")
        sys.exit(1)

    log.info("NVIS Redfish BMC Exporter starting")
    log.info("  BMC targets    : %d host(s) — %s", len(BMC_HOSTS),
             ", ".join(BMC_HOSTS[:5]) + ("..." if len(BMC_HOSTS) > 5 else ""))
    log.info("  BMC user       : %s", BMC_USER)
    log.info("  Cluster        : %s", CLUSTER_NAME or "(unset)")
    log.info("  DGX flavor hint: %s", DGX_FLAVOR or "(auto-detect)")
    log.info("  Concurrency    : %d BMCs, %d req/BMC", BMC_CONCURRENCY, SENSOR_CONCURRENCY)
    log.info("  Scrape interval: %ds", SCRAPE_INTERVAL)
    log.info("  SSL verify     : %s", VERIFY_SSL)
    log.info("  Listen port    : %d", PORT)

    t = threading.Thread(target=scrape_loop, daemon=True)
    t.start()

    server = HTTPServer(("", PORT), Handler)
    log.info("Metrics available at http://0.0.0.0:%d/metrics", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
