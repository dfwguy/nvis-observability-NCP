#!/usr/bin/env python3
"""
ib-identity generator — the topology/identity JOIN BRIDGE for the NVIS IB
observability suite.

Reads the ground-truth artifacts:
  - expected-topology.json  (per-edge cls + optic + su; nodes carry role/rack/su)
  - placement_map.json      (host -> [su, rack, ru, node_in_rack])
  - ufm_switches_new.csv     (ip, name(=guid), system_name(hostname), role, model, fw, sw, guid)

Emits value=1 info-metrics that carry the FULL canonical label set so every raw
per-port / per-link series (node_infiniband_*, ufm_*, ib_cable_issue, ...) can be
label-joined back to the expected topology, and blast-radius / topology consumers
can resolve any switch/link/host identity without fuzzy matching.

Metric families:
  ib_switch_info{hostname,system_name,guid,ip,role,model,su,rack}      1 per switch
  ib_link_peer{link_id,a_node,a_port,z_node,z_port,cls,a_su,z_su,a_optic,z_optic}
                                                                        1 per edge
  ib_host_rail_leaf{node,device,rail_idx,leaf,leaf_port,su,rack,ru}    1 per host-leaf
  ib_leaf_spine{leaf,leaf_port,spine,spine_port,su}                    1 per leaf-spine
  ib_leaf_leaf{a_leaf,a_port,z_leaf,z_port,a_su,z_su}                  1 per leaf-leaf
  ib_node_placement{node,su,rack,unit,ru,node_in_rack}                 1 per host

Also writes, from the SAME source of truth:
  alias.json       — host <-> system_name <-> guid <-> ip <-> rail <-> rack/su
  blast-index.json — compact adjacency for a Cytoscape iframe (client-side)

This module is import-safe: build_all() returns (metrics_text, alias, blast_index,
counts) so the exporter can call it in-process; run as a script to write files to
disk.

Environment variables:
  IB_IDENTITY_DATA  directory holding the artifacts (default ./data)
  IB_IDENTITY_OUT   directory for file output when run as a script (default: here)
  CLUSTER_NAME      cluster label   (default: cluster)
  FABRIC            fabric label    (default: fabric)
  HOST_NAME_PREFIX  canonical host hostname prefix (default: gpu)
"""

import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("IB_IDENTITY_DATA", os.path.join(HERE, "data"))

CLUSTER = os.environ.get("CLUSTER_NAME", "cluster")
FABRIC = os.environ.get("FABRIC", "fabric")
# Canonical host hostnames start with this prefix (e.g. "gpu" -> gpu000001).
# Anything matching it resolves to itself; other host-side identifiers are
# rewritten via the workbook resolver below.
HOST_NAME_PREFIX = os.environ.get("HOST_NAME_PREFIX", "gpu")

# Workbook server-profile name, e.g. "SU5-R3012-Server-1". Encodes the rack
# (R<rack>) and the 1-based ordinal of the host within that rack (Server-<n>).
# It is NOT the canonical hostname and never joins to node_infiniband_* which
# are keyed by node="<host>". See build_host_resolver().
_WORKBOOK_HOST_RE = re.compile(r"^SU\d+-R(\d+)-Server-(\d+)$")

TOPO_FILE = os.path.join(DATA_DIR, "expected-topology.json")
PLACE_FILE = os.path.join(DATA_DIR, "placement_map.json")
UFM_FILE = os.path.join(DATA_DIR, "ufm_switches_new.csv")


# ---------------------------------------------------------------------------
# Prometheus text-format helpers
# ---------------------------------------------------------------------------
def _esc(v):
    """Escape a Prometheus label value."""
    if v is None:
        return ""
    s = str(v)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _line(metric, labels):
    """Render one `metric{k="v",...} 1` line with cluster/fabric stamped on."""
    labels = dict(labels)
    labels.setdefault("cluster", CLUSTER)
    labels.setdefault("fabric", FABRIC)
    # deterministic label ordering -> stable output / diffable
    parts = ",".join('{}="{}"'.format(k, _esc(labels[k])) for k in sorted(labels))
    return "{}{{{}}} 1".format(metric, parts)


def _rail_idx(device):
    """mlx5_0 -> 0 ... mlx5_3 -> 3 ; anything else -> ''."""
    if device and device.startswith("mlx5_"):
        tail = device[len("mlx5_"):]
        if tail.isdigit():
            return tail
    return ""


# ---------------------------------------------------------------------------
# Load sources
# ---------------------------------------------------------------------------
def load_sources():
    with open(TOPO_FILE) as fh:
        topo = json.load(fh)
    with open(PLACE_FILE) as fh:
        placement = json.load(fh)
    ufm = {}
    with open(UFM_FILE) as fh:
        for row in csv.DictReader(fh):
            ufm[row["system_name"]] = row
    return topo, placement, ufm


# ---------------------------------------------------------------------------
# Canonical host-name resolver
# ---------------------------------------------------------------------------
def build_host_resolver(placement):
    """Return a fn mapping ANY host-side identifier to the canonical host name.

    Host-leaf edges in expected-topology.json (and the topology's own host
    nodes) sometimes carry the workbook server-profile name
    ("SU<x>-R<rack>-Server-<n>") instead of the canonical hostname. That name
    never joins to node_infiniband_* (keyed on node="<host>"), so it must be
    rewritten.

    placement_map.json is the authoritative host inventory: it maps every
    canonical hostname to [su, rack, ru, node_in_rack], covering ALL racks
    (including any workbook-profile racks the topology never assigned a host name
    to). "Server-<n>" is exactly the node_in_rack ordinal, so we resolve on
    (rack, node_in_rack). We also index (rack, unit) as a fallback.
    """
    by_rack_nir = {}    # (rack, node_in_rack) -> host
    by_rack_unit = {}   # (rack, unit)         -> host
    for host, rec in placement.items():
        su, rack, ru, nir = (rec + ["", "", "", ""])[:4]
        by_rack_nir[(str(rack), str(nir))] = host
        unit = str(ru)[1:] if str(ru).startswith("U") else str(ru)
        by_rack_unit[(str(rack), unit)] = host

    def resolve(name):
        """canonical host -> itself; workbook name -> canonical; else None."""
        s = str(name)
        if s.startswith(HOST_NAME_PREFIX):
            return s
        m = _WORKBOOK_HOST_RE.match(s)
        if m:
            rack, ordinal = m.group(1), m.group(2)
            return by_rack_nir.get((rack, ordinal)) or by_rack_unit.get((rack, str(10 + int(ordinal))))
        return None

    return resolve


# ---------------------------------------------------------------------------
# Build metrics + alias + blast-index in one pass
# ---------------------------------------------------------------------------
def build_all():
    topo, placement, ufm = load_sources()

    nodes = topo["nodes"]
    edges = topo["edges"]
    node_by_id = {n["id"]: n for n in nodes}
    resolve_host = build_host_resolver(placement)
    unresolved_hosts = []          # host-side names that could not be canonicalised

    lines = []
    counts = {}

    def emit(metric, labels):
        lines.append(_line(metric, labels))
        counts[metric] = counts.get(metric, 0) + 1

    # ---- ib_switch_info (one per leaf/spine node; enrich w/ UFM guid/ip/fw) ----
    alias_switches = {}
    guid_to_name = {}
    ip_to_name = {}
    for n in nodes:
        if n["role"] not in ("leaf", "spine", "ufm"):
            continue
        sysname = n["id"]
        u = ufm.get(sysname, {})
        guid = u.get("guid", "")
        ip = u.get("ip", "")
        emit("ib_switch_info", {
            "hostname": sysname,
            "system_name": sysname,
            "guid": guid,
            "ip": ip,
            "role": n["role"],
            "model": u.get("model", "") or n.get("model", ""),
            "fw_version": u.get("fw_version", ""),
            "sw_version": u.get("sw_version", ""),
            "state": u.get("state", ""),
            "su": n.get("su", ""),
            "rack": n.get("rack", ""),
        })
        alias_switches[sysname] = {
            "guid": guid, "ip": ip, "role": n["role"],
            "model": u.get("model", "") or n.get("model", ""),
            "su": n.get("su", ""), "rack": n.get("rack", ""),
        }
        if guid:
            guid_to_name[guid] = sysname
        if ip:
            ip_to_name[ip] = sysname

    # ---- ib_switch_info for LIVE UFM switches absent from the topology ----
    # These are switches UFM reports that the workbook has no placement for.
    # They carry real guid/ip and must still be joinable from switch-side
    # counters (UFM Telemetry keyed by guid), so we emit identity for them even
    # though the workbook has no placement.
    for sysname, u in ufm.items():
        if sysname in alias_switches:
            continue
        guid = u.get("guid", "")
        ip = u.get("ip", "")
        emit("ib_switch_info", {
            "hostname": sysname,
            "system_name": sysname,
            "guid": guid,
            "ip": ip,
            "role": u.get("role", ""),          # UFM-reported (core/tor)
            "model": u.get("model", ""),
            "fw_version": u.get("fw_version", ""),
            "sw_version": u.get("sw_version", ""),
            "state": u.get("state", ""),
            "su": "",
            "rack": "",
            "topology_match": "false",          # flag: not in expected-topology
        })
        alias_switches[sysname] = {
            "guid": guid, "ip": ip, "role": u.get("role", ""),
            "model": u.get("model", ""), "su": "", "rack": "",
            "topology_match": False,
        }
        if guid:
            guid_to_name[guid] = sysname
        if ip:
            ip_to_name[ip] = sysname

    # ---- ib_node_placement (one per host) ----
    # Keyed off placement_map.json, the authoritative host inventory: it is
    # fully canonical for ALL hosts, whereas the topology's own host nodes may
    # carry a workbook server-profile name for some racks. Emitting from
    # placement guarantees node="<host>" everywhere.
    alias_hosts = {}
    for host in sorted(placement):
        su, rack, ru, nin = (placement[host] + ["", "", "", ""])[:4]
        unit = str(ru)[1:] if str(ru).startswith("U") else str(ru)
        emit("ib_node_placement", {
            "node": host,
            "su": su,
            "rack": rack,
            "unit": unit,
            "ru": ru,
            "node_in_rack": nin,
        })
        alias_hosts[host] = {
            "su": su, "rack": rack, "unit": unit,
            "ru": ru, "node_in_rack": nin, "rails": {},
        }

    # ---- edge-driven families ----
    blast = {
        "meta": {
            "cluster": CLUSTER, "fabric": FABRIC,
            "node_count": len(nodes), "edge_count": len(edges),
        },
        "edges": {},          # edge_id -> {a_node,a_port,z_node,z_port,cls,a_su,z_su}
        "leaf_hosts": {},     # leaf -> [ [host, device, rail_idx], ... ]
        "leaf_spines": {},    # leaf -> [spine, ...]
        "spine_leaves": {},   # spine -> [leaf, ...]
        "leaf_leaves": {},    # leaf -> [leaf, ...]
        "host_rails": {},     # host -> {device: leaf}
    }

    for e in edges:
        link_id = e["id"]
        a_node, a_port = e["a_node"], e["a_port"]
        z_node, z_port = e["z_node"], e["z_port"]
        cls = e["cls"]
        a_su, z_su = e.get("a_su", ""), e.get("z_su", "")

        # Normalise the host endpoint of host-leaf edges to the canonical
        # hostname. a_node is the host side for this class; the workbook
        # server-profile name never joins to node_infiniband_*. This rewrite
        # fixes BOTH ib_link_peer (host-leaf class) and ib_host_rail_leaf, plus
        # the blast-index, from one place.
        if cls == "host-leaf":
            canon = resolve_host(a_node)
            if canon is None:
                unresolved_hosts.append((link_id, a_node))
                # do not emit host-keyed identity we cannot join; log & skip
                continue
            a_node = canon

        # ib_link_peer — one per edge (the universal edge identity)
        emit("ib_link_peer", {
            "link_id": link_id,
            "a_node": a_node, "a_port": a_port,
            "z_node": z_node, "z_port": z_port,
            "cls": cls,
            "a_su": a_su, "z_su": z_su,
            "a_optic": e.get("a_optic", ""),
            "z_optic": e.get("z_optic", ""),
        })
        blast["edges"][link_id] = {
            "a_node": a_node, "a_port": a_port,
            "z_node": z_node, "z_port": z_port,
            "cls": cls, "a_su": a_su, "z_su": z_su,
        }

        if cls == "host-leaf":
            # a_node = host, a_port = mlx5_x ; z_node = leaf, z_port = swNpM
            host, device, leaf, leaf_port = a_node, a_port, z_node, z_port
            ph = alias_hosts.get(host, {})
            su = ph.get("su", a_su)
            rack = ph.get("rack", "")
            ru = ph.get("ru", "")
            ridx = _rail_idx(device)
            emit("ib_host_rail_leaf", {
                "node": host, "device": device, "rail_idx": ridx,
                "leaf": leaf, "leaf_port": leaf_port,
                "su": su, "rack": rack, "ru": ru,
            })
            blast["leaf_hosts"].setdefault(leaf, []).append([host, device, ridx])
            blast["host_rails"].setdefault(host, {})[device] = leaf
            if host in alias_hosts:
                alias_hosts[host]["rails"][device] = leaf

        elif cls == "leaf-spine":
            leaf, leaf_port, spine, spine_port = a_node, a_port, z_node, z_port
            emit("ib_leaf_spine", {
                "leaf": leaf, "leaf_port": leaf_port,
                "spine": spine, "spine_port": spine_port,
                "su": a_su,
            })
            blast["leaf_spines"].setdefault(leaf, []).append(spine)
            blast["spine_leaves"].setdefault(spine, []).append(leaf)

        elif cls == "leaf-leaf":
            emit("ib_leaf_leaf", {
                "a_leaf": a_node, "a_port": a_port,
                "z_leaf": z_node, "z_port": z_port,
                "a_su": a_su, "z_su": z_su,
            })
            blast["leaf_leaves"].setdefault(a_node, []).append(z_node)
            blast["leaf_leaves"].setdefault(z_node, []).append(a_node)

        # leaf-ufm: captured in ib_link_peer only (informational SM attach)

    if unresolved_hosts:
        sys.stderr.write(
            "[ib-identity] WARNING: {} host-leaf edge(s) had an unresolvable "
            "host endpoint (skipped, NOT emitted):\n".format(len(unresolved_hosts)))
        for link_id, name in unresolved_hosts[:50]:
            sys.stderr.write("  {} a_node={!r}\n".format(link_id, name))
        if len(unresolved_hosts) > 50:
            sys.stderr.write("  ... and {} more\n".format(len(unresolved_hosts) - 50))

    metrics_text = "\n".join([
        "# HELP ib_switch_info Switch identity/role join bridge (topology + UFM inventory).",
        "# TYPE ib_switch_info gauge",
        "# HELP ib_link_peer Physical link endpoint identity (one per expected edge).",
        "# TYPE ib_link_peer gauge",
        "# HELP ib_host_rail_leaf Host rail (mlx5_x) -> leaf mapping; joins node_infiniband_* on (node,device).",
        "# TYPE ib_host_rail_leaf gauge",
        "# HELP ib_leaf_spine Leaf -> spine uplink (capacity tier).",
        "# TYPE ib_leaf_spine gauge",
        "# HELP ib_leaf_leaf Leaf <-> leaf cross-SU trunk.",
        "# TYPE ib_leaf_leaf gauge",
        "# HELP ib_node_placement Host placement (su/rack/unit/ru) join carrier.",
        "# TYPE ib_node_placement gauge",
    ] + lines) + "\n"

    alias = {
        "meta": {"cluster": CLUSTER, "fabric": FABRIC,
                 "hosts": len(alias_hosts), "switches": len(alias_switches)},
        "hosts": alias_hosts,
        "switches": alias_switches,
        "guid_to_name": guid_to_name,
        "ip_to_name": ip_to_name,
    }

    # de-dup / stabilise adjacency lists in the blast index
    for d in (blast["leaf_spines"], blast["spine_leaves"], blast["leaf_leaves"]):
        for k in d:
            d[k] = sorted(set(d[k]))

    return metrics_text, alias, blast, counts


def main():
    out_dir = os.environ.get("IB_IDENTITY_OUT", HERE)
    metrics_text, alias, blast, counts = build_all()

    with open(os.path.join(out_dir, "metrics.prom"), "w") as fh:
        fh.write(metrics_text)
    with open(os.path.join(out_dir, "alias.json"), "w") as fh:
        json.dump(alias, fh, separators=(",", ":"), sort_keys=True)
    with open(os.path.join(out_dir, "blast-index.json"), "w") as fh:
        json.dump(blast, fh, separators=(",", ":"), sort_keys=True)

    total = sum(counts.values())
    sys.stderr.write("ib-identity generated series counts:\n")
    for k in sorted(counts):
        sys.stderr.write("  {:<20} {}\n".format(k, counts[k]))
    sys.stderr.write("  {:<20} {}\n".format("TOTAL", total))
    return counts


if __name__ == "__main__":
    main()
