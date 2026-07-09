#!/usr/bin/env python3
"""parse_nvlmapper.py — switch-side intra-rack NVLink DOWN-lane mapping.

Turns the NVLMapper "midfile" output into Prometheus gauges. NVLMapper is a
vendor-supplied tool (NOT shipped in this repo) that logs into each NVLink rack's
NVSwitch tray BMCs, walks the ACP port map and emits, per rack, a text file listing
the intra-rack NVLink lanes that are DOWN.

Input glob : {output_root}/nvlmapper/**/midfile__acp_down_connections.txt
             (per-rack subdirs like .../nvlmapper/R1001/ OR flat; the rack id is also
             read from a sibling NVLINK_CC_PORTMAP_<RACK>.xlsx token — the .xlsx
             itself is NOT parsed/opened).
Output     : Prometheus exposition text. Gauges (present ONLY for down lanes):
             nvl_lane_down{cluster,[flavor,]rack,tray,acp_port,cartridge,cage,lane} 1
             nvl_lane_down_total{cluster,[flavor,]rack}      = #down lanes on that rack
                                                               (0 == healthy pre-flight gate)
             nvlmapper_source_mtime_seconds{cluster,[flavor,]rack} = max mtime of that
                                                               rack's midfile(s)
Example in : "SW1 acp53 connects to CC1_C5_Lane_15"
             -> tray=SW1 acp_port=acp53 cartridge=CC1 cage=C5 lane=15
                (rack from subdir/xlsx token, else "unknown")
Contract   : stdlib-only, collect() NEVER raises (torn/partial NFS files are skipped),
             stateless gauges.

Site identity comes from the environment (NO hardcoded cluster/flavor):
  CLUSTER_NAME | NVLMAPPER_CLUSTER   Prometheus cluster= label   (default "cluster")
  NVLINK_FLAVOR                      NVLink fabric flavor label   (default "" -> omitted)
  NVLMAPPER_OUTPUT_ROOT              root that contains nvlmapper/ (default "/data")
"""

import os
import re
import sys

# --- site identity (env-driven; never hardcode a customer name) --------------------------
CLUSTER = (os.environ.get("CLUSTER_NAME")
           or os.environ.get("NVLMAPPER_CLUSTER")
           or "cluster")
FLAVOR = os.environ.get("NVLINK_FLAVOR", "").strip()
DEFAULT_OUTPUT_ROOT = os.environ.get("NVLMAPPER_OUTPUT_ROOT", "/data")

# --- line grammar (liberal: separators/casing may vary; require the key tokens) ----------
# head: <tray> <sep> acp<N>   e.g. "SW1 acp53" / "sw1: acp 53"
_RE_HEAD = re.compile(r'^\s*(?P<tray>\w+)\W+(?P<acp>acp\s*\d+)', re.IGNORECASE)
# dest: CC<N> .. C<N> .. Lane .. <N>   e.g. "CC1_C5_Lane_15" / "cc1 c5 lane 15"
_RE_DEST = re.compile(
    r'(?P<cartridge>CC\s*\d+)[\s_\-]+(?P<cage>C\s*\d+)[\s_\-]+Lane[\s_\-]*(?P<lane>\d+)',
    re.IGNORECASE)
# rack path component / token, e.g. R1001..R1008, R2001..R2008
_RE_RACK = re.compile(r'[Rr]\d{3,4}')
_RE_RACK_FULL = re.compile(r'[Rr]\d{3,4}\Z')
_RE_XLSX = re.compile(r'NVLINK_CC_PORTMAP_.*\.xlsx\Z', re.IGNORECASE)
_RE_XLSX_TOK = re.compile(r'NVLINK_CC_PORTMAP_([^_]+)', re.IGNORECASE)


def _log(msg):
    try:
        sys.stderr.write("[parse_nvlmapper] " + msg + "\n")
    except Exception:
        pass


def _esc(v):
    """Escape a Prometheus label value."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _base_labels():
    """Common leading labels for every series: cluster (+ flavor when configured)."""
    b = 'cluster="%s"' % _esc(CLUSTER)
    if FLAVOR:
        b += ',flavor="%s"' % _esc(FLAVOR)
    return b


def _is_midfile(fn):
    low = fn.lower()
    return low.endswith(".txt") and "acp_down_connections" in low


def _derive_rack(base, dirpath, fname):
    """rack = R#### subdir (any ancestor under nvlmapper) -> sibling xlsx token -> filename token -> 'unknown'."""
    # 1. a rack-shaped path component between base and the file
    try:
        rel = os.path.relpath(dirpath, base).replace("\\", "/")
    except Exception:
        rel = ""
    for comp in rel.split("/"):
        if _RE_RACK_FULL.match(comp):
            return comp.upper()
    # 2. a sibling NVLINK_CC_PORTMAP_<RACK>.xlsx (do NOT open it; token only)
    try:
        for sib in sorted(os.listdir(dirpath)):
            if _RE_XLSX.match(sib):
                rm = _RE_RACK.search(sib)
                if rm:
                    return rm.group(0).upper()
                tm = _RE_XLSX_TOK.match(sib)
                if tm and tm.group(1):
                    return tm.group(1)
    except Exception:
        pass
    # 3. a rack token embedded in the midfile filename itself
    fm = _RE_RACK.search(fname)
    if fm:
        return fm.group(0).upper()
    return "unknown"


def _parse_line(line):
    """Return a list of (tray, acp_port, cartridge, cage, lane) tuples — one per DOWN-lane
    destination on the line — or None if the line is not a down-lane line at all.

    A single head (tray + acp port) may fan out to multiple CC*_C*_Lane_* destinations on
    one line, e.g. "SW1 acp53 connects to CC1_C5_Lane_15 and CC2_C6_Lane_7" -> both lanes.
    Using finditer (not a single search) prevents silently dropping the 2nd+ destination and
    undercounting nvl_lane_down_total.
    """
    mh = _RE_HEAD.match(line)
    if not mh:
        return None
    tray = mh.group("tray").upper()
    acp_n = re.sub(r"\D", "", mh.group("acp"))
    if not (tray and acp_n):
        return None
    out = []
    for md in _RE_DEST.finditer(line):
        cart_n = re.sub(r"\D", "", md.group("cartridge"))
        cage_n = re.sub(r"\D", "", md.group("cage"))
        lane = re.sub(r"\D", "", md.group("lane"))
        if not (cart_n and cage_n and lane):
            continue
        out.append((tray, "acp" + acp_n, "CC" + cart_n, "C" + cage_n, lane))
    if not out:
        return None
    return out


def _process_file(path, base, rack_lanes, rack_mtime, rack_seen):
    """Parse one midfile; may raise on torn/partial/unreadable file (caller skips it)."""
    dirpath = os.path.dirname(path)
    fname = os.path.basename(path)
    rack = _derive_rack(base, dirpath, fname)

    try:
        mt = os.path.getmtime(path)
    except Exception:
        mt = None

    # A torn NFS write can leave undecodable bytes; errors="replace" keeps us from raising,
    # and any garbled line simply fails the grammar below and is skipped.
    with open(path, "r", errors="replace") as f:
        text = f.read()

    # file was readable -> this rack has a valid pre-flight midfile (even if 0 down lanes)
    rack_seen.add(rack)
    rack_lanes.setdefault(rack, set())
    if mt is not None and (rack not in rack_mtime or mt > rack_mtime[rack]):
        rack_mtime[rack] = mt

    file_down = 0  # DOWN-lane destinations parsed from THIS file (for the rack=unknown safety WARN)
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_line(line)
        if not parsed:
            _log("skip %s:%d no-match: %s" % (fname, lineno, line[:120]))
            continue
        for tup in parsed:
            rack_lanes[rack].add(tup)
            file_down += 1

    # Safety-relevant: a DOWN lane that lands under rack="unknown" is INVISIBLE to the
    # rack-scoped pre-flight gate count(nvl_lane_down{rack=~"<job racks>"})==0 — a silent
    # false-negative (job could launch on a rack with a down lane while the gate reads green).
    # Flag it loudly so the operator/dashboard falls back to an unscoped safeguard such as
    # sum(nvl_lane_down_total)>0 or count(nvl_lane_down{rack="unknown"})==0.
    if rack == "unknown" and file_down > 0:
        _log('WARN rack=unknown for %s carries %d DOWN lane(s) with no R#### subdir/xlsx: '
             'these are INVISIBLE to the rack-scoped gate count(nvl_lane_down{rack=~"..."})==0 '
             '(silent false-negative). Gate additionally on sum(nvl_lane_down_total)>0.'
             % (fname, file_down))


def _collect(output_root):
    base = os.path.join(output_root, "nvlmapper")
    rack_lanes = {}   # rack -> set of (tray, acp_port, cartridge, cage, lane)
    rack_mtime = {}   # rack -> max mtime (float)
    rack_seen = set()  # racks whose midfile was successfully read

    files = []
    try:
        if os.path.isdir(base):
            for dirpath, _dirs, filenames in os.walk(base):
                for fn in filenames:
                    try:
                        if _is_midfile(fn):
                            files.append(os.path.join(dirpath, fn))
                    except Exception:
                        continue
    except Exception as e:
        _log("walk failed under %s: %r" % (base, e))
        files = []

    for path in sorted(files):
        try:
            _process_file(path, base, rack_lanes, rack_mtime, rack_seen)
        except Exception as e:
            # torn/partial/unreadable — skip this file, keep going (NFS writes aren't atomic)
            _log("skip file %s: %r" % (path, e))
            continue

    if not rack_seen:
        return ""

    bl = _base_labels()
    out = []
    out.append("# HELP nvl_lane_down Switch-side intra-rack NVLink DOWN lane (present only when down); value 1.")
    out.append("# TYPE nvl_lane_down gauge")
    for rack in sorted(rack_lanes):
        for (tray, acp, cart, cage, lane) in sorted(rack_lanes[rack]):
            out.append(
                'nvl_lane_down{%s,rack="%s",tray="%s",acp_port="%s",cartridge="%s",cage="%s",lane="%s"} 1'
                % (bl, _esc(rack), _esc(tray), _esc(acp), _esc(cart), _esc(cage), _esc(lane)))

    out.append("# HELP nvl_lane_down_total Count of DOWN NVLink lanes on the rack (0 = healthy pre-flight gate). Gauge despite the _total name.")
    out.append("# TYPE nvl_lane_down_total gauge")
    for rack in sorted(rack_seen):
        total = len(rack_lanes.get(rack, ()))
        out.append('nvl_lane_down_total{%s,rack="%s"} %d' % (bl, _esc(rack), total))

    out.append("# HELP nvlmapper_source_mtime_seconds Max mtime (unix seconds) of the rack's parsed midfile(s); proves blank != healthy.")
    out.append("# TYPE nvlmapper_source_mtime_seconds gauge")
    for rack in sorted(rack_mtime):
        out.append('nvlmapper_source_mtime_seconds{%s,rack="%s"} %d'
                   % (bl, _esc(rack), int(rack_mtime[rack])))

    return "\n".join(out) + "\n"


def collect(output_root: str = None) -> str:
    """Parse nvlmapper down-lane midfiles under {output_root}/nvlmapper and return Prometheus text.

    Never raises: any unexpected error returns "" (or whatever was built). Gauges only, idempotent.
    """
    if output_root is None:
        output_root = DEFAULT_OUTPUT_ROOT
    try:
        return _collect(output_root)
    except Exception as e:
        _log("fatal, returning empty: %r" % e)
        return ""


if __name__ == "__main__":
    print(collect(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT_ROOT))
