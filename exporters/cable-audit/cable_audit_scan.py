#!/usr/bin/env python3
"""
NVIS IB cabling live audit — scan + diff + run-over-run compare generator.

Produces the same `cable-issue_<ts>.csv` schema that
`ib_cable_issue_exporter.py` consumes, but LIVE: it discovers the actual
per-port neighbors of every NVOS switch (either from the UFM Subnet Manager's
active-link view, or over SSH), diffs them against the expected cable map
(`expected-topology.json`, derived from the site cabling BOM), and writes:

  * cable-issue_<UTCSTAMP>.csv          main snapshot (exporter input)
        Node Description,Timestamp,Rack,Unit,Issue,
        Source Switch Port,Expected Neighbor,Discovered Neighbor
  * cable-issue_<DATE>_Target.csv       repatch worklist (Wrong-neighbor + Wrong-port)
  * cable-issue-delta_<UTCSTAMP>.csv    run-over-run change list (change=new|resolved)
  * cable_audit.prom                    Prometheus textfile (freshness + change aggregates)

Run-over-run comparison uses the stable key
    (node, source_port, issue, expected_neighbor, discovered_neighbor)
over the miswiring rows only (the same rows the exporter turns into
`ib_cable_issue`); NEW = current - previous, RESOLVED = previous - current.
This answers "which miscablings were FIXED and which are NEW" every audit cycle.

Modes
  ufm (default)   Pull the SM's active-link view (/resources/links). The SM sees
                  every ACTIVE IB link, so it does NOT suffer the LLDP blindness
                  that turns live IB ports into false "Media Unplugged".
  ssh             Legacy per-switch SSH + NEIGHBOR_CMD (lldp). Kept for
                  fallback / air-gapped UFM; blind to IB fabric ports. Reads
                  switch SSH creds from the environment (SWITCH_SSH_USER /
                  SWITCH_SSH_PASSWORD).
  dry-run         (DRY_RUN=1 or --dry-run) skip SSH/UFM; read a discovered-topology
                  fixture (--fixture) instead — used to validate the diff engine.
  validate        (--validate) build a reconstruction fixture from a known
                  baseline CSV, run the diff, and (optionally) assert expected
                  counts supplied via --expect-* flags / env.

Credentials policy: passwords come ONLY from the environment. No password literal
is ever written, printed, or defaulted here.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import ssl
import sys
import urllib.request
from base64 import b64encode
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config (env-driven)
# ---------------------------------------------------------------------------
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/data")
EXPECTED_TOPOLOGY = os.environ.get(
    "EXPECTED_TOPOLOGY", os.path.join(OUTPUT_DIR, "expected-topology.json")
)
SWITCH_MAP_JSON = os.environ.get(
    "SWITCH_MAP_JSON", os.path.join(OUTPUT_DIR, "switch_su_map.json")
)
INVENTORY_CSV = os.environ.get(
    "INVENTORY_CSV", os.path.join(OUTPUT_DIR, "ufm_switches_new.csv")
)
FABRIC = os.environ.get("FABRIC", "fabric")

# Discovery source for the "actual" per-port neighbor map:
#   ufm  (default) — pull the SM's active-link view (/resources/links). The SM
#                    sees every ACTIVE IB link, so it does NOT suffer the LLDP
#                    blindness that turns live IB ports into false "Media
#                    Unplugged".
#   ssh            — legacy per-switch SSH + NEIGHBOR_CMD (lldp). Kept for
#                    fallback / air-gapped UFM; blind to IB fabric ports.
SCAN_MODE = os.environ.get("SCAN_MODE", "ufm").strip().lower()

# UFM REST (SM view). Creds from the .env the installer writes.
UFM_HOST = os.environ.get("UFM_HOST", "").strip()
UFM_USER = os.environ.get("UFM_USER", "").strip()
UFM_PASS = os.environ.get("UFM_PASSWORD") or os.environ.get("UFM_PASS", "")
UFM_VERIFY_SSL = os.environ.get("UFM_VERIFY_SSL", "0") == "1"
UFM_TIMEOUT = int(os.environ.get("UFM_TIMEOUT", "60"))
# Offline dev/test overrides: read a saved snapshot instead of hitting UFM.
UFM_LINKS_FILE = os.environ.get("UFM_LINKS_FILE", "").strip()
UFM_SYSTEMS_FILE = os.environ.get("UFM_SYSTEMS_FILE", "").strip()

# Switch SSH creds (ssh mode only). SWITCH_SSH_* are canonical; NVOS_* accepted.
SWITCH_SSH_USER = os.environ.get("SWITCH_SSH_USER") or os.environ.get("NVOS_USERNAME")
SWITCH_SSH_PASSWORD = os.environ.get("SWITCH_SSH_PASSWORD") or os.environ.get("NVOS_PASSWORD")

# Optional switch-hostname prefixes used as a fallback to classify a link
# endpoint as a switch when it is not already in switch_map or the UFM live
# systems set (e.g. leaf/spine naming like "leaf,spine" or your site's codes).
# Empty (default) => rely solely on switch_map + UFM live systems.
SWITCH_NAME_PREFIXES = tuple(
    p.strip() for p in os.environ.get("SWITCH_NAME_PREFIXES", "").split(",") if p.strip()
)

SSH_WORKERS = int(os.environ.get("SSH_WORKERS", "12"))
SSH_TIMEOUT = int(os.environ.get("SSH_TIMEOUT", "20"))
RETAIN = int(os.environ.get("RETAIN", "8"))
CHANGE_CAP = int(os.environ.get("CHANGE_CAP", "2000"))
PROM_FILE = os.environ.get("PROM_FILE", os.path.join(OUTPUT_DIR, "cable_audit.prom"))

# The neighbor-discovery command run ON each switch (structured JSON preferred).
# LLDP covers eth0 + any LLDP-carrying ports; on-switch IB discovery supplements
# the sw<N>p<M> fabric ports. parse_discovered() is tolerant of both shapes.
NEIGHBOR_CMD = os.environ.get(
    "NEIGHBOR_CMD",
    "lldpcli show neighbors -f json 2>/dev/null || "
    "nv show interface link lldp -o json 2>/dev/null",
)

MISWIRE_ISSUES = ("Media Unplugged", "Extra-cable", "Wrong-neighbor", "Wrong-port")
UNREACHABLE_ISSUE = "Unreachable-device"

# Host HCAs that LINKED but UFM could not resolve to a hostname show up with a
# raw device description ("... ConnectX8 ... Mellanox Technologies") instead of a
# host name. Such a port is CORRECTLY cabled (the SM sees an active XDR link) —
# the host is merely unregistered — so it must NOT be scored as Wrong-neighbor.
# We adopt the expected identity for the diff and surface these separately as
# ib_cable_unidentified_device (the "Connected-but-Unidentified GPU" panel).
UNIDENTIFIED_DEVICES = []  # [{switch, port, su, guid, device}], populated by scan_ufm


def _unidentified_kind(peer_name):
    p = (peer_name or "").lower()
    if "connectx" in p:
        return "connectx8" if ("connectx8" in p or "connectx-8" in p) else "connectx"
    return ""
MAIN_HEADER = [
    "Node Description", "Timestamp", "Rack", "Unit", "Issue",
    "Source Switch Port", "Expected Neighbor", "Discovered Neighbor",
]

# Switch-side port aliases: the BOM may name the UFM uplink port "FNM" while the
# NVOS scan reports it as "PFNM"; canonicalise so they compare equal.
PORT_ALIASES = {"PFNM": "FNM"}

# Edge classes NOT counted as "expected" cabling. Many BOMs treat leaf<->leaf
# cables as unexpected (they surface as Extra-cable in the audit); the default
# below matches that convention. Override with EXCLUDE_EDGE_CLASSES="" to include
# them, or a comma-separated list to exclude other classes.
EXCLUDE_EDGE_CLASSES = {
    c.strip() for c in os.environ.get("EXCLUDE_EDGE_CLASSES", "leaf-leaf").split(",")
    if c.strip()
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def norm_node(name: str) -> str:
    """Case-insensitive, domain-stripped node identity for comparison/output."""
    return (name or "").strip().split(".")[0].lower()


def canon_port(port: str) -> str:
    p = (port or "").strip()
    return PORT_ALIASES.get(p.upper(), p)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp_file(dt):
    return dt.strftime("%Y-%m-%d_%H-%M-%S")


def stamp_main(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def stamp_target(dt):
    # M/D/YY H:MM  (exporter._parse_ts accepts %m/%d/%y %H:%M)
    return "%d/%d/%s %d:%02d" % (dt.month, dt.day, dt.strftime("%y"), dt.hour, dt.minute)


# ---------------------------------------------------------------------------
# Expected map + inventory
# ---------------------------------------------------------------------------
def load_switch_map(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_expected(path, switch_names):
    """
    Build EXP[switch][canon_port] = (remote_node, remote_port) by indexing BOTH
    endpoints of every edge whose endpoint is a switch (leaf-spine / leaf-leaf
    have switches on both sides).  Returns (EXP, node_loc) where
    node_loc[norm_node] = (rack, unit).
    """
    with open(path, "r", encoding="utf-8") as fh:
        topo = json.load(fh)
    sw = {norm_node(s) for s in switch_names}
    node_loc = {}
    for n in topo.get("nodes", []):
        node_loc[norm_node(n["id"])] = (str(n.get("rack", "")), str(n.get("unit", "")))
    exp = defaultdict(dict)
    for e in topo.get("edges", []):
        if e.get("cls") in EXCLUDE_EDGE_CLASSES:
            continue
        a, ap, z, zp = e["a_node"], e["a_port"], e["z_node"], e["z_port"]
        if norm_node(a) in sw:
            exp[norm_node(a)][canon_port(ap)] = (norm_node(z), zp)
        if norm_node(z) in sw:
            exp[norm_node(z)][canon_port(zp)] = (norm_node(a), ap)
    return exp, node_loc


def load_inventory(path):
    """norm_node(name) -> mgmt ip, from ufm_switches_new.csv (system_name col)."""
    name_ip = {}
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                nm = norm_node(r.get("system_name") or r.get("name") or "")
                ip = (r.get("ip") or "").strip()
                if nm and ip:
                    name_ip[nm] = ip
    except FileNotFoundError:
        eprint(f"[warn] inventory {path} not found; live scan will have no IPs")
    return name_ip


# ---------------------------------------------------------------------------
# Discovered topology — LIVE (paramiko) and DRY-RUN (fixture)
# ---------------------------------------------------------------------------
def parse_discovered(raw_stdout):
    """
    Tolerant parser: turns the switch's neighbor dump into
    {local_port(canon): (remote_node, remote_port) or None}.

    Accepts (a) lldpcli `-f json`  {lldp:{interface:[{<port>:{chassis:{name|
    id:{value}}, port:{id:{value}}}}]}}, (b) NVOS `nv show ... -o json`
    {<port>:{...LLDP fields...}}, and (c) whitespace "port remote_sys remote_port"
    fallback lines.  Empty / absent neighbor -> None.
    """
    disc = {}
    txt = (raw_stdout or "").strip()
    if not txt:
        return disc

    def _chassis_name(ch):
        if not isinstance(ch, dict):
            return norm_node(str(ch))
        if "name" in ch:
            v = ch["name"]
            if isinstance(v, list) and v:
                return norm_node(v[0].get("value", ""))
            if isinstance(v, dict):
                return norm_node(v.get("value", ""))
            return norm_node(v)
        if "id" in ch and isinstance(ch["id"], dict):
            return norm_node(ch["id"].get("value", ""))
        for k in ch:  # by-name form {"<host>": {...}}
            return norm_node(k)
        return ""

    def _port_id(p):
        if isinstance(p, dict):
            pid = p.get("id", p)
            if isinstance(pid, dict):
                return str(pid.get("value", "")).strip()
            if isinstance(pid, list) and pid:
                return str(pid[0].get("value", "")).strip()
            return str(pid).strip()
        return str(p).strip()

    try:
        parsed = json.loads(txt)
    except (ValueError, TypeError):
        parsed = None

    if isinstance(parsed, dict) and "lldp" in parsed:
        ifaces = parsed["lldp"].get("interface", [])
        if isinstance(ifaces, dict):
            ifaces = [ifaces]
        for entry in ifaces:
            for local_port, info in (entry or {}).items():
                rn = _chassis_name(info.get("chassis", {}))
                rp = _port_id(info.get("port", {}))
                disc[canon_port(local_port)] = (rn, rp) if rn else None
        return disc

    if isinstance(parsed, dict):  # NVOS by-port form
        for local_port, info in parsed.items():
            if not isinstance(info, dict):
                continue
            rn = norm_node(
                info.get("remote-system-name")
                or info.get("system-name")
                or info.get("SystemName")
                or _chassis_name(info.get("chassis", {}))
            )
            rp = (info.get("remote-port") or info.get("port-id")
                  or _port_id(info.get("port", {})))
            disc[canon_port(local_port)] = (rn, str(rp).strip()) if rn else None
        return disc

    for line in txt.splitlines():  # whitespace fallback
        parts = line.split()
        if len(parts) >= 3:
            disc[canon_port(parts[0])] = (norm_node(parts[1]), parts[2])
    return disc


def scan_one_live(name, ip, username, password):
    """SSH one switch, run NEIGHBOR_CMD, return (name, reachable, ports)."""
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(
            ip, username=username, password=password,
            timeout=SSH_TIMEOUT, banner_timeout=SSH_TIMEOUT,
            auth_timeout=SSH_TIMEOUT, look_for_keys=False, allow_agent=False,
        )
        _in, out, _err = cli.exec_command(NEIGHBOR_CMD, timeout=SSH_TIMEOUT)
        raw = out.read().decode("utf-8", "replace")
        return name, True, parse_discovered(raw)
    except Exception as exc:  # noqa: BLE001 — any failure == unreachable
        eprint(f"[scan] {name} ({ip}) unreachable: {exc}")
        return name, False, {}
    finally:
        try:
            cli.close()
        except Exception:  # noqa: BLE001
            pass


def scan_live(switch_map, inventory):
    """
    Returns DISC: {switch: {"reachable": bool, "ports": {port:(node,port)|None}}}.
    Only switches with a mgmt IP are attempted; creds strictly from env.
    """
    username = SWITCH_SSH_USER
    password = SWITCH_SSH_PASSWORD
    if not username or not password:
        eprint("[fatal] SWITCH_SSH_USER / SWITCH_SSH_PASSWORD not set in "
               "environment. Refusing to scan.")
        sys.exit(2)
    targets = []
    for name in switch_map:
        ip = inventory.get(norm_node(name))
        if ip:
            targets.append((norm_node(name), ip))
        else:
            eprint(f"[scan] {name}: no mgmt IP in inventory -> not attempted")
    disc = {}
    with ThreadPoolExecutor(max_workers=SSH_WORKERS) as ex:
        futs = {ex.submit(scan_one_live, n, ip, username, password): n
                for n, ip in targets}
        for fut in as_completed(futs):
            name, reachable, ports = fut.result()
            disc[name] = {"reachable": reachable, "ports": ports}
    return disc


def scan_dryrun(fixture_path):
    """
    Read a discovered-topology fixture and normalise into the DISC shape.

    Fixture JSON:
      {"switches": {
          "<switch>": {"reachable": true,
                       "neighbors": {"<port>": "<node>:<port>" | null | "NONE"}},
          "<switch>": {"reachable": false}
      }}
    """
    with open(fixture_path, "r", encoding="utf-8") as fh:
        fx = json.load(fh)
    switches = fx.get("switches", fx)
    disc = {}
    for name, spec in switches.items():
        if not isinstance(spec, dict):
            continue
        ports = {}
        for port, val in (spec.get("neighbors") or {}).items():
            if val in (None, "", "NONE", "NA"):
                ports[canon_port(port)] = None
            else:
                node, _, rport = str(val).partition(":")
                ports[canon_port(port)] = (norm_node(node), rport)
        disc[norm_node(name)] = {"reachable": bool(spec.get("reachable", True)),
                                 "ports": ports}
    return disc


# ---------------------------------------------------------------------------
# Discovered topology — UFM SM link view (/resources/links)
# ---------------------------------------------------------------------------
def _ufm_get(path):
    """GET a UFM REST resource, return parsed JSON. Honors the *_FILE overrides
    for offline development (no network)."""
    override = {"resources/links": UFM_LINKS_FILE,
                "resources/systems": UFM_SYSTEMS_FILE}.get(path, "")
    if override:
        with open(override, "r", encoding="utf-8") as fh:
            return json.load(fh)
    if not (UFM_HOST and UFM_USER):
        eprint("[fatal] UFM_HOST / UFM_USER not set and no *_FILE override. "
               "Refusing to scan.")
        sys.exit(2)
    url = f"https://{UFM_HOST}/ufmRest/{path}"
    tok = b64encode(f"{UFM_USER}:{UFM_PASS}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {tok}"})
    ctx = ssl.create_default_context()
    if not UFM_VERIFY_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=UFM_TIMEOUT, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _peer_name(node_description):
    """'<host>:ibB3D0F0HCA0p1s3' -> '<host>';  '' / Aggregation Node -> ''."""
    nm = norm_node((node_description or "").split(":")[0])
    if not nm or "aggregation node" in (node_description or "").lower():
        return ""
    return nm


def _peer_port(node_description, fallback):
    d = node_description or ""
    return (d.split(":", 1)[1].strip() if ":" in d else (fallback or "").strip())


def scan_ufm(exp, switch_map):
    """
    Build DISC (same shape as scan_live) from the UFM SM active-link view.

    Two reconciliations make the SM data join to the swNpM expected map:
      * host-facing leaf ports — UFM already names them swNpM; the peer NODE is
        authoritative, but the peer PORT differs (BOM 'mlx5_2' vs UFM HCA string)
        so we ADOPT the expected port when the node matches (kills false
        Wrong-port; still catches Wrong-neighbor + Media Unplugged).
      * switch<->switch (leaf-spine) ports — UFM names the leaf side 'A#/#', NOT
        swNpM, so a per-port join is impossible. Instead we count ACTIVE links
        per (leaf,spine) pair and satisfy that many of the leaf's expected
        spine-facing swNpM ports; the remaining expected ports stay absent (=>
        real Media Unplugged), and any surplus becomes Extra-cable. This
        reconciles the COUNT without an A#/#->swNpM map.

    Only switches that UFM actually manages (live fabric) are audited; planned-
    but-not-built switches are skipped (never counted as faults).
    """
    links = _ufm_get("resources/links")
    systems = _ufm_get("resources/systems")
    UNIDENTIFIED_DEVICES.clear()

    fabric = {norm_node(k) for k in switch_map}                 # planned universe
    live = {norm_node(s.get("system_name") or s.get("name") or "")
            for s in systems if s.get("type") == "switch"}
    audited = sorted((live & fabric) or live)                   # live switches only

    def is_switch(n):
        return (n in fabric or n in live
                or (bool(SWITCH_NAME_PREFIXES) and n.startswith(SWITCH_NAME_PREFIXES)))

    disc = {sw: {"reachable": True, "ports": {}} for sw in audited}
    adj = defaultdict(Counter)                                  # leaf -> Counter(spine)
    seen_pair = set()                                           # dedupe undirected sw-sw links

    for l in links:
        a = _peer_name(l.get("source_port_node_description"))
        b = _peer_name(l.get("destination_port_node_description"))
        ap = canon_port(l.get("source_port_dname") or "")
        bp = _peer_port(l.get("destination_port_node_description"),
                        l.get("destination_port_dname"))
        if not a and not b:
            continue
        a_sw, b_sw = is_switch(a), is_switch(b)
        a_guid = str(l.get("source_guid") or l.get("source_port_name", "").split("_")[0])
        b_guid = str(l.get("destination_guid") or l.get("destination_port_name", "").split("_")[0])
        # record each fabric-switch endpoint's own port, keyed by its own dname
        for node, port, peer, pport, peer_is_sw, peer_guid in (
                (a, ap, b, bp, b_sw, b_guid), (b, bp, a, ap, a_sw, a_guid)):
            if node not in disc or not port:
                continue
            if peer_is_sw:
                continue  # switch-facing handled via adjacency below, not per-port
            if not peer:
                continue  # unnamed peer (SHARP Aggregation Node / FNM) — not a cable
            e = exp.get(node, {}).get(canon_port(port))
            kind = _unidentified_kind(peer)
            if kind:
                # Correctly cabled, host just unregistered in UFM. Record it and,
                # if a host was expected here, adopt that identity so the diff scores
                # the port as correct (not a false Wrong-neighbor).
                UNIDENTIFIED_DEVICES.append(
                    {"switch": node, "port": canon_port(port), "guid": peer_guid,
                     "device": kind})
                if e:
                    disc[node]["ports"][canon_port(port)] = (e[0], e[1])
                continue
            # host-facing: adopt expected peer-port when the node matches
            if e and norm_node(peer) == norm_node(e[0]):
                pport = e[1]
            disc[node]["ports"][canon_port(port)] = (peer, pport)
        # switch<->switch adjacency (count once per physical link)
        if a_sw and b_sw and a and b:
            key = (frozenset((a, b)), ap, bp)
            if key in seen_pair:
                continue
            seen_pair.add(key)
            if a in disc:
                adj[a][b] += 1
            if b in disc:
                adj[b][a] += 1

    # Reconcile switch-facing expected ports against the per-pair active counts.
    for leaf in disc:
        by_peer = defaultdict(list)                            # spine -> [(swNpM, exp_port)]
        for p, (peer, pport) in exp.get(leaf, {}).items():
            if is_switch(norm_node(peer)):
                by_peer[norm_node(peer)].append((p, pport))
        for spine, ports in by_peer.items():
            have = adj[leaf].get(spine, 0) if spine in live else 0
            ports.sort()
            for i, (p, pport) in enumerate(ports):
                if i < have:
                    disc[leaf]["ports"][p] = (spine, pport)   # active -> mark correct
                # else: leave absent -> genuine Media Unplugged in diff()
        # surplus active links to a spine beyond what the BOM expects -> Extra-cable
        for spine, cnt in adj[leaf].items():
            need = len(by_peer.get(spine, []))
            for j in range(need, cnt):
                disc[leaf]["ports"][f"xtra:{spine}:{j}"] = (spine, "?")

    return disc


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------
def diff(exp, node_loc, switch_map, disc, ts_main):
    """
    Produce main-CSV rows (list of dict keyed by MAIN_HEADER).  Per attempted
    switch: one blank heartbeat row (audited denominator); an Unreachable-device
    row if SSH failed; else one row per miswired port.
    """
    rows = []
    # case-insensitive switch metadata lookup
    smap = {norm_node(k): v for k, v in switch_map.items()}

    def loc_of(node):
        return node_loc.get(norm_node(node), ("", ""))

    def exp_str(en, ep):
        r, u = loc_of(en)
        return f"{en}:{ep} (rack:{r}, unit:{u})"

    def src_rack_unit(sw):
        ent = smap.get(norm_node(sw), {})
        return str(ent.get("rack", "")), str(ent.get("unit", ""))

    def mkrow(node, issue, sp, en, dn):
        rk, un = src_rack_unit(node)
        return {
            "Node Description": node, "Timestamp": ts_main, "Rack": rk, "Unit": un,
            "Issue": issue, "Source Switch Port": sp,
            "Expected Neighbor": en, "Discovered Neighbor": dn,
        }

    for sw in sorted(disc):
        state = disc[sw]
        rows.append(mkrow(sw, "", "", "", ""))  # heartbeat for every attempted switch
        if not state.get("reachable", False):
            rows.append(mkrow(sw, UNREACHABLE_ISSUE, "NA", "NA", "NA"))
            continue
        ex_ports = exp.get(sw, {})
        di_ports = state.get("ports", {})
        for p in sorted(set(ex_ports) | set(di_ports)):
            e = ex_ports.get(p)   # (en, ep) or None
            d = di_ports.get(p)   # (dn, dp) or None
            sp = f"{sw}:{p}"
            if e and not d:
                rows.append(mkrow(sw, "Media Unplugged", sp, exp_str(*e), "NONE"))
            elif d and not e:
                rows.append(mkrow(sw, "Extra-cable", sp, "NONE", f"{d[0]}:{d[1]}"))
            elif e and d:
                en, ep = e
                dn, dp = d
                if norm_node(dn) == norm_node(en) and dp == ep:
                    continue  # correct
                elif norm_node(dn) == norm_node(en):
                    rows.append(mkrow(sw, "Wrong-port", sp, exp_str(en, ep), f"{dn}:{dp}"))
                else:
                    rows.append(mkrow(sw, "Wrong-neighbor", sp, exp_str(en, ep), f"{dn}:{dp}"))
    return rows


# ---------------------------------------------------------------------------
# CSV / delta / prom writers + run-over-run compare
# ---------------------------------------------------------------------------
def miswire_rows(rows):
    return [r for r in rows if r["Issue"] in MISWIRE_ISSUES]


def row_key(r):
    """Stable comparison key: the specific fault on a physical port."""
    return (r["Node Description"], r["Source Switch Port"], r["Issue"],
            r["Expected Neighbor"], r["Discovered Neighbor"])


def read_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(fh)]


def pick_previous(out_dir, exclude_path):
    cands = [p for p in glob.glob(os.path.join(out_dir, "cable-issue_*.csv"))
             if not p.endswith("_Target.csv")
             and os.path.abspath(p) != os.path.abspath(exclude_path or "")]
    if not cands:
        return ""
    return max(cands, key=lambda p: (os.path.basename(p), os.path.getmtime(p)))


def write_main_csv(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MAIN_HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def write_target_csv(path, rows, ts_target):
    tgt = [r for r in rows if r["Issue"] in ("Wrong-neighbor", "Wrong-port")]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MAIN_HEADER, extrasaction="ignore")
        w.writeheader()
        for r in tgt:
            row = dict(r)
            row["Timestamp"] = ts_target
            w.writerow(row)
    os.replace(tmp, path)
    return len(tgt)


def compute_delta(cur_rows, prev_rows):
    cur = {row_key(r): r for r in miswire_rows(cur_rows)}
    prv = {row_key(r): r for r in (miswire_rows(prev_rows) if prev_rows else [])}
    new = [cur[k] for k in cur.keys() - prv.keys()]
    resolved = [prv[k] for k in prv.keys() - cur.keys()]
    return new, resolved


def write_delta_csv(path, new, resolved):
    header = ["change"] + MAIN_HEADER
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for tag, recs in (("new", new), ("resolved", resolved)):
            for r in recs:
                row = {"change": tag}
                row.update({k: r.get(k, "") for k in MAIN_HEADER})
                w.writerow(row)
    os.replace(tmp, path)


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def su_of(switch_map, node):
    ent = switch_map.get(node)
    if not isinstance(ent, dict):
        for k, v in switch_map.items():
            if norm_node(k) == norm_node(node):
                ent = v
                break
    return ent.get("su", "") if isinstance(ent, dict) else ""


def write_prom(path, switch_map, cur_rows, new, resolved, ts_now, prev_ts, compare_available):
    smap = {norm_node(k): v for k, v in switch_map.items()}

    def su(node):
        ent = smap.get(norm_node(node), {})
        return ent.get("su", "") if isinstance(ent, dict) else ""

    cur_mis = miswire_rows(cur_rows)
    cur_ct = Counter((su(r["Node Description"]), r["Issue"]) for r in cur_mis)
    new_ct = Counter((su(r["Node Description"]), r["Issue"]) for r in new)
    res_ct = Counter((su(r["Node Description"]), r["Issue"]) for r in resolved)
    suppressed = 1 if (len(new) + len(resolved)) > CHANGE_CAP else 0

    lines = []
    a = lines.append
    a("# HELP ib_cable_audit_timestamp_seconds Unix time of the newest cable scan.")
    a("# TYPE ib_cable_audit_timestamp_seconds gauge")
    a(f"ib_cable_audit_timestamp_seconds {ts_now:.0f}")
    a("# HELP ib_cable_audit_age_seconds Age of the scan at write time (0; a live collector recomputes).")
    a("# TYPE ib_cable_audit_age_seconds gauge")
    a("ib_cable_audit_age_seconds 0")
    a("# HELP ib_cable_audit_previous_timestamp_seconds Unix time of the previous scan used in the diff.")
    a("# TYPE ib_cable_audit_previous_timestamp_seconds gauge")
    a(f"ib_cable_audit_previous_timestamp_seconds {prev_ts:.0f}")
    a("# HELP ib_cable_audit_compare_available 1 if a previous snapshot was diffable.")
    a("# TYPE ib_cable_audit_compare_available gauge")
    a(f"ib_cable_audit_compare_available {compare_available}")
    a("# HELP ib_cable_audit_change_suppressed 1 if per-item change series were capped.")
    a("# TYPE ib_cable_audit_change_suppressed gauge")
    a(f"ib_cable_audit_change_suppressed {suppressed}")

    a("# HELP ib_cable_issue_current_total Current miswiring count by SU and issue.")
    a("# TYPE ib_cable_issue_current_total gauge")
    for (s, issue), n in sorted(cur_ct.items()):
        a(f'ib_cable_issue_current_total{{su="{_esc(s)}",issue="{_esc(issue)}"}} {n}')
    a("# HELP ib_cable_issue_new_total Miswirings that appeared since the previous scan.")
    a("# TYPE ib_cable_issue_new_total gauge")
    for (s, issue), n in sorted(new_ct.items()):
        a(f'ib_cable_issue_new_total{{su="{_esc(s)}",issue="{_esc(issue)}"}} {n}')
    a("# HELP ib_cable_issue_resolved_total Miswirings fixed since the previous scan.")
    a("# TYPE ib_cable_issue_resolved_total gauge")
    for (s, issue), n in sorted(res_ct.items()):
        a(f'ib_cable_issue_resolved_total{{su="{_esc(s)}",issue="{_esc(issue)}"}} {n}')

    a("# HELP ib_cable_unidentified_device Host port with an ACTIVE link to a device "
      "UFM could not name (e.g. an unregistered ConnectX-8 GPU rail). Correctly "
      "cabled; the host is just not yet identified in UFM.")
    a("# TYPE ib_cable_unidentified_device gauge")
    a("# HELP ib_cable_unidentified_total Count of unidentified linked devices by SU and device type.")
    a("# TYPE ib_cable_unidentified_total gauge")
    uni_ct = Counter()
    for d in UNIDENTIFIED_DEVICES:
        s = su(d["switch"])
        uni_ct[(s, d["device"])] += 1
        a('ib_cable_unidentified_device{'
          f'switch="{_esc(d["switch"])}",port="{_esc(d["port"])}",'
          f'su="{_esc(s)}",guid="{_esc(d["guid"])}",device="{_esc(d["device"])}"' '} 1')
    for (s, dev), n in sorted(uni_ct.items()):
        a(f'ib_cable_unidentified_total{{su="{_esc(s)}",device="{_esc(dev)}"}} {n}')

    a("# HELP ib_cable_issue_change Per-changed-port series (change=new|resolved).")
    a("# TYPE ib_cable_issue_change gauge")
    if not suppressed:
        for tag, recs in (("new", new), ("resolved", resolved)):
            for r in recs:
                node = r["Node Description"]
                a('ib_cable_issue_change{'
                  f'node="{_esc(node)}",su="{_esc(su(node))}",'
                  f'source_port="{_esc(r["Source Switch Port"])}",'
                  f'issue="{_esc(r["Issue"])}",'
                  f'expected_neighbor="{_esc(r["Expected Neighbor"])}",'
                  f'discovered_neighbor="{_esc(r["Discovered Neighbor"])}",'
                  f'change="{tag}"' '} 1')

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def prune(out_dir, pattern, keep):
    files = sorted(glob.glob(os.path.join(out_dir, pattern)),
                   key=lambda p: (os.path.basename(p), os.path.getmtime(p)), reverse=True)
    for p in files[keep:]:
        try:
            os.remove(p)
            eprint(f"[prune] removed {os.path.basename(p)}")
        except OSError as exc:
            eprint(f"[prune] could not remove {p}: {exc}")


def ts_from_name(path):
    m = re.search(r"cable-issue_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})",
                  os.path.basename(path or ""))
    if not m:
        return 0.0
    return datetime(*[int(x) for x in m.groups()], tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Validation: reconstruct a fixture from a baseline CSV and re-diff it
# ---------------------------------------------------------------------------
def build_reconstruction_fixture(baseline_csv, exp, switch_map):
    """
    Reconstruct the discovered topology that would REPRODUCE `baseline_csv`:
    for every attempted switch, start from the expected map (all ports correct)
    then apply the baseline's problem rows.  Unreachable switches -> reachable
    false.  Used only to validate the diff engine against known counts.
    """
    rows = read_csv_rows(baseline_csv)
    attempted, unreachable = set(), set()
    problems = defaultdict(list)  # sw -> [(canon_port, issue, discovered), ...]
    for r in rows:
        sw = norm_node(r.get("Node Description", ""))
        issue = (r.get("Issue") or "").strip()
        if not sw:
            continue
        attempted.add(sw)
        if issue == "":
            continue
        if issue == UNREACHABLE_ISSUE:
            unreachable.add(sw)
            continue
        sp = (r.get("Source Switch Port") or "").strip()
        port = canon_port(sp.split(":", 1)[1]) if ":" in sp else canon_port(sp)
        disc = (r.get("Discovered Neighbor") or "").strip()
        problems[sw].append((port, issue, disc))

    switches = {}
    for sw in sorted(attempted):
        if sw in unreachable:
            switches[sw] = {"reachable": False}
            continue
        neigh = {}
        for p, (en, ep) in exp.get(sw, {}).items():
            neigh[p] = f"{en}:{ep}"                 # correct baseline
        for port, issue, disc in problems[sw]:
            if issue == "Media Unplugged":
                neigh[port] = None
            else:  # Extra-cable / Wrong-neighbor / Wrong-port
                neigh[port] = disc if disc and disc != "NONE" else None
        switches[sw] = {"reachable": True, "neighbors": neigh}
    return {"meta": {"reconstructed_from": os.path.basename(baseline_csv)},
            "switches": switches}


def validate(baseline_csv, switch_map_path, expected_path, fixture_out, expect):
    """Reconstruction round-trip. `expect` is an optional dict:
       {switch, wrong_neighbor, extra_cable, total, su, su_total} — when the
       switch key is present the counts are asserted (PASS/FAIL); otherwise the
       function just reports the regenerated-vs-baseline category table."""
    switch_map = load_switch_map(switch_map_path)
    exp, node_loc = load_expected(expected_path, switch_map.keys())
    fixture = build_reconstruction_fixture(baseline_csv, exp, switch_map)
    os.makedirs(os.path.dirname(fixture_out) or ".", exist_ok=True)
    with open(fixture_out, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh)
    disc = scan_dryrun(fixture_out)
    # Reuse the baseline's own first timestamp so the round-trip is stable.
    base = read_csv_rows(baseline_csv)
    ts_main = (base[0].get("Timestamp") if base else "") or stamp_main(utc_now())
    rows = diff(exp, node_loc, switch_map, disc, ts_main)

    mis = miswire_rows(rows)
    by_sw = Counter(r["Node Description"] for r in mis)
    by_sw_issue = Counter((r["Node Description"], r["Issue"]) for r in mis)
    su_total = Counter(su_of(switch_map, r["Node Description"]) for r in mis)
    cat = Counter(r["Issue"] for r in mis)

    base_cat = Counter((r.get("Issue") or "").strip() for r in base
                       if (r.get("Issue") or "").strip() in MISWIRE_ISSUES)

    heartbeats = sum(1 for r in rows if r["Issue"] == "")
    unreach = sum(1 for r in rows if r["Issue"] == UNREACHABLE_ISSUE)

    print("=== DRY-RUN VALIDATION (reconstruction round-trip) ===")
    print(f"attempted switches (heartbeat rows): {heartbeats}   unreachable rows: {unreach}")
    print(f"total miswiring rows: regenerated={len(mis)}  baseline={sum(base_cat.values())}")
    print("category counts  regenerated -> baseline:")
    for c in MISWIRE_ISSUES:
        flag = "" if cat[c] == base_cat[c] else "  <-- DIFF"
        print(f"   {c:16s} {cat[c]:5d} -> {base_cat[c]:5d}{flag}")

    ok = (cat == base_cat)  # the core round-trip invariant
    if expect.get("switch"):
        tgt = norm_node(expect["switch"])
        wn = by_sw_issue[(tgt, "Wrong-neighbor")]
        ec = by_sw_issue[(tgt, "Extra-cable")]
        tt = by_sw[tgt]
        print(f"{expect['switch']}: Wrong-neighbor={wn} Extra-cable={ec} TOTAL={tt}")
        checks = []
        if expect.get("wrong_neighbor") is not None:
            checks.append(wn == expect["wrong_neighbor"])
        if expect.get("extra_cable") is not None:
            checks.append(ec == expect["extra_cable"])
        if expect.get("total") is not None:
            checks.append(tt == expect["total"])
        if expect.get("su") and expect.get("su_total") is not None:
            st = su_total[expect["su"]]
            print(f"{expect['su']} total: {st}  (expected {expect['su_total']})")
            checks.append(st == expect["su_total"])
        ok = ok and all(checks)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# Main generate flow
# ---------------------------------------------------------------------------
def generate(dry_run, fixture_path):
    switch_map = load_switch_map(SWITCH_MAP_JSON)
    exp, node_loc = load_expected(EXPECTED_TOPOLOGY, switch_map.keys())

    now = utc_now()
    ts_now = now.timestamp()
    ts_main = stamp_main(now)
    ts_target = stamp_target(now)
    fstamp = stamp_file(now)

    if dry_run:
        if not fixture_path or not os.path.exists(fixture_path):
            eprint("[fatal] --dry-run requires --fixture <discovered-topology.json>")
            sys.exit(2)
        disc = scan_dryrun(fixture_path)
    elif SCAN_MODE == "ufm":
        disc = scan_ufm(exp, switch_map)
        if not disc:
            eprint("[fatal] UFM returned 0 live switches — refusing to overwrite the good snapshot")
            sys.exit(1)
    else:
        inventory = load_inventory(INVENTORY_CSV)
        disc = scan_live(switch_map, inventory)
        if not any(v.get("reachable") for v in disc.values()):
            eprint("[fatal] 0 switches reachable — refusing to overwrite the good snapshot")
            sys.exit(1)

    rows = diff(exp, node_loc, switch_map, disc, ts_main)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    main_path = os.path.join(OUTPUT_DIR, f"cable-issue_{fstamp}.csv")
    target_path = os.path.join(OUTPUT_DIR, f"cable-issue_{now.strftime('%Y-%m-%d')}_Target.csv")
    delta_path = os.path.join(OUTPUT_DIR, f"cable-issue-delta_{fstamp}.csv")

    prev_path = pick_previous(OUTPUT_DIR, exclude_path=main_path)
    prev_rows = read_csv_rows(prev_path) if prev_path else []
    prev_ts = ts_from_name(prev_path) if prev_path else 0.0
    compare_available = 1 if prev_path else 0

    if compare_available:
        new, resolved = compute_delta(rows, prev_rows)
    else:
        # First run (no previous snapshot): do NOT flag the whole file as NEW.
        # The first real NEW/RESOLVED numbers appear on the next run.
        new, resolved = [], []

    write_main_csv(main_path, rows)
    n_tgt = write_target_csv(target_path, rows, ts_target)
    write_delta_csv(delta_path, new, resolved)
    write_prom(PROM_FILE, switch_map, rows, new, resolved, ts_now, prev_ts, compare_available)

    prune(OUTPUT_DIR, "cable-issue_*-*-*_*-*-*.csv", RETAIN)
    prune(OUTPUT_DIR, "cable-issue-delta_*.csv", RETAIN)

    mis = miswire_rows(rows)
    print(f"wrote {main_path}  (rows={len(rows)}, miswire={len(mis)})")
    print(f"wrote {target_path} (worklist={n_tgt})")
    print(f"wrote {delta_path}  (new={len(new)}, resolved={len(resolved)}, "
          f"compare_available={compare_available})")
    print(f"wrote {PROM_FILE}")


def main():
    ap = argparse.ArgumentParser(description="IB cabling live scan + diff + compare")
    ap.add_argument("--dry-run", action="store_true", help="skip SSH/UFM; read --fixture instead")
    ap.add_argument("--fixture", default=os.environ.get("FIXTURE", ""),
                    help="discovered-topology fixture JSON for --dry-run")
    ap.add_argument("--validate", action="store_true",
                    help="reconstruct from a baseline CSV and check the round-trip")
    ap.add_argument("--baseline", default=os.environ.get("BASELINE_CSV", ""),
                    help="baseline CSV for --validate")
    ap.add_argument("--fixture-out",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "fixtures", "reconstructed-from-baseline.json"),
                    help="where to write the reconstruction fixture (--validate)")
    # Optional per-site expected counts for --validate (defaults: no assertion).
    ap.add_argument("--expect-switch", default=os.environ.get("VALIDATE_SWITCH", ""))
    ap.add_argument("--expect-wrong-neighbor", type=int, default=None)
    ap.add_argument("--expect-extra-cable", type=int, default=None)
    ap.add_argument("--expect-total", type=int, default=None)
    ap.add_argument("--expect-su", default=os.environ.get("VALIDATE_SU", ""))
    ap.add_argument("--expect-su-total", type=int, default=None)
    args = ap.parse_args()

    if args.validate:
        if not args.baseline:
            eprint("[fatal] --validate requires --baseline <cable-issue_*.csv> "
                   "(or BASELINE_CSV env)")
            sys.exit(2)
        expect = {
            "switch": args.expect_switch,
            "wrong_neighbor": args.expect_wrong_neighbor,
            "extra_cable": args.expect_extra_cable,
            "total": args.expect_total,
            "su": args.expect_su,
            "su_total": args.expect_su_total,
        }
        ok = validate(args.baseline, SWITCH_MAP_JSON, EXPECTED_TOPOLOGY,
                      args.fixture_out, expect)
        sys.exit(0 if ok else 1)

    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"
    generate(dry, args.fixture)


if __name__ == "__main__":
    main()
