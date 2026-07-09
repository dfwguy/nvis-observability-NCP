#!/usr/bin/env python3
"""NVIS NetQ Prometheus exporter (optional network-fabric integration).

Polls the NVIDIA Cumulus NetQ REST API and exposes a Prometheus
/metrics endpoint covering switch inventory, events, links and
interface counters. See README.md for the full env-var contract.

This exporter is OPTIONAL — enable it only when the cluster's Ethernet /
Cumulus fabric is managed by NVIDIA NetQ. Clusters without NetQ can skip it.

Required env: NETQ_ENDPOINT (base URL) plus either NETQ_TOKEN or
NETQ_USER + NETQ_PASS. All site values come from the environment; nothing
is hardcoded.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
import urllib3
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("netq-exporter")

# NETQ_ENDPOINT is the documented name; NETQ_URL is accepted as an alias.
NETQ_URL = os.environ.get("NETQ_ENDPOINT", os.environ.get("NETQ_URL", "")).rstrip("/")
NETQ_TOKEN = os.environ.get("NETQ_TOKEN", "").strip()
NETQ_USER = os.environ.get("NETQ_USER", "").strip()
NETQ_PASS = os.environ.get("NETQ_PASS", "")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "")
PORT = int(os.environ.get("PORT", "9109"))
SCRAPE_INTERVAL = max(5, int(os.environ.get("SCRAPE_INTERVAL", "30")))
VERIFY_SSL = os.environ.get("VERIFY_SSL", "true").lower() == "true"
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "15"))

if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_PREFIX = "/netq/api/v1"
LOGIN_PATH = f"{API_PREFIX}/auth/login"
DEVICES_PATH = f"{API_PREFIX}/endpoints/devices"
EVENTS_PATH = f"{API_PREFIX}/endpoints/events"
LINKS_PATH = f"{API_PREFIX}/endpoints/links"
INTERFACES_PATH = f"{API_PREFIX}/endpoints/interfaces"

REGISTRY = CollectorRegistry()
COMMON_LABELS = ["cluster"]

def _g(name: str, h: str, labels: list[str]) -> Gauge:
    return Gauge(name, h, labels, registry=REGISTRY)

def _c(name: str, h: str, labels: list[str]) -> Counter:
    return Counter(name, h, labels, registry=REGISTRY)


_HOST_PORT = COMMON_LABELS + ["hostname", "port"]
netq_up = _g("netq_up", "1 if last NetQ poll succeeded.", COMMON_LABELS)
netq_scrape_duration_seconds = _g(
    "netq_scrape_duration_seconds", "Duration of last NetQ poll.", COMMON_LABELS)
netq_scrape_errors_total = _c(
    "netq_scrape_errors_total", "NetQ API errors.", COMMON_LABELS + ["endpoint"])
netq_device_info = _g(
    "netq_device_info", "Cumulus switch metadata (always 1).",
    COMMON_LABELS + ["hostname", "model", "version", "asic", "platform"])
netq_device_up = _g(
    "netq_device_up", "1 if NetQ reports switch as fresh/up.",
    COMMON_LABELS + ["hostname"])
netq_event_count = _c(
    "netq_event_count", "Count of NetQ events by severity/type.",
    COMMON_LABELS + ["severity", "type"])
netq_link_up = _g(
    "netq_link_up", "1 if link is up between src/dst.",
    COMMON_LABELS + ["src", "src_port", "dst", "dst_port"])
netq_interface_bytes_in = _c(
    "netq_interface_bytes_in", "Ingress bytes per interface.", _HOST_PORT)
netq_interface_bytes_out = _c(
    "netq_interface_bytes_out", "Egress bytes per interface.", _HOST_PORT)
netq_interface_oper_up = _g(
    "netq_interface_oper_up", "1 if interface oper state is up.", _HOST_PORT)

# Per-(host, port) last-seen byte counters so we emit deltas to the
# Prometheus Counter rather than absolute values.
_byte_state: dict[tuple[str, str], tuple[float, float]] = {}
# Bounded set of recent event IDs so lingering events are not double-counted.
_seen_events: set[str] = set()
_MAX_SEEN_EVENTS = 4096


# HTTP session
class NetQClient:
    """Thin wrapper around requests.Session that handles NetQ auth."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.verify = VERIFY_SSL
        self.session.headers.update({"Accept": "application/json"})
        self._token: str | None = NETQ_TOKEN or None
        self._token_lock = threading.Lock()

    def _login(self) -> str:
        if not (NETQ_USER and NETQ_PASS):
            raise RuntimeError("no NETQ_TOKEN provided and NETQ_USER/NETQ_PASS are empty")
        url = urljoin(NETQ_URL + "/", LOGIN_PATH.lstrip("/"))
        log.info("authenticating against NetQ as user=%s", NETQ_USER)
        resp = self.session.post(
            url, json={"username": NETQ_USER, "password": NETQ_PASS},
            timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        # NetQ documents both "access_token" and "token" depending on release.
        token = body.get("access_token") or body.get("token") or body.get("id_token") or ""
        if not token:
            raise RuntimeError(f"login response missing token: keys={list(body)}")
        return token

    def _auth_header(self) -> dict[str, str]:
        with self._token_lock:
            if not self._token:
                self._token = self._login()
            return {"Authorization": f"Bearer {self._token}"}

    def _invalidate_token(self) -> None:
        # Only force re-login if the token came from a username/password login.
        with self._token_lock:
            if not NETQ_TOKEN:
                self._token = None

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = urljoin(NETQ_URL + "/", path.lstrip("/"))
        for attempt in (0, 1):
            headers = self._auth_header()
            resp = self.session.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            if resp.status_code in (401, 403) and attempt == 0:
                log.warning("auth failed (%s) on %s; refreshing token", resp.status_code, path)
                self._invalidate_token()
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else []
        return []


# Field helpers
def _first(d: dict[str, Any], *keys: str, default: str = "") -> str:
    """Return the first non-empty value for any of the keys, else default."""
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default


def _as_iterable(payload: Any) -> Iterable[dict[str, Any]]:
    """NetQ returns either a bare list or a dict with a 'data'/'items' key."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "events", "devices"):
            v = payload.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


# Pollers
def poll_devices(client: NetQClient) -> None:
    payload = client.get(DEVICES_PATH)
    rows = list(_as_iterable(payload))
    log.info("devices: %d rows", len(rows))
    netq_device_info.clear()
    netq_device_up.clear()
    for row in rows:
        hostname = _first(row, "hostname", "name", "device")
        if not hostname:
            continue
        model = _first(row, "model", "platform_model")
        version = _first(row, "version", "os_version", "sys_version")
        asic = _first(row, "asic", "asic_model", "chip")
        platform = _first(row, "platform", "vendor")
        netq_device_info.labels(
            cluster=CLUSTER_NAME, hostname=hostname, model=model,
            version=version, asic=asic, platform=platform,
        ).set(1)
        status = _first(row, "status", "device_status", "agent_status").lower()
        fresh = row.get("fresh")
        if isinstance(fresh, bool):
            up = 1 if fresh else 0
        elif status in {"down", "rotten", "offline"}:
            up = 0
        else:
            up = 1  # default: present in inventory => up
        netq_device_up.labels(cluster=CLUSTER_NAME, hostname=hostname).set(up)


def poll_events(client: NetQClient) -> None:
    payload = client.get(EVENTS_PATH)
    rows = list(_as_iterable(payload))
    log.info("events: %d rows", len(rows))
    for row in rows:
        ev_id = _first(row, "id", "event_id", "uuid")
        if ev_id:
            if ev_id in _seen_events:
                continue
            _seen_events.add(ev_id)
            if len(_seen_events) > _MAX_SEEN_EVENTS:
                for old in list(_seen_events)[: _MAX_SEEN_EVENTS // 2]:
                    _seen_events.discard(old)
        severity = _first(row, "severity", "level", default="info").lower()
        ev_type = _first(row, "type", "message_type", "event_type", default="generic")
        netq_event_count.labels(
            cluster=CLUSTER_NAME, severity=severity, type=ev_type
        ).inc()


def poll_links(client: NetQClient) -> None:
    payload = client.get(LINKS_PATH)
    rows = list(_as_iterable(payload))
    log.info("links: %d rows", len(rows))
    netq_link_up.clear()
    for row in rows:
        src = _first(row, "src_hostname", "src", "source", "hostname")
        dst = _first(row, "dst_hostname", "dst", "destination", "peer")
        src_port = _first(row, "src_port", "src_interface", "ifname")
        dst_port = _first(row, "dst_port", "dst_interface", "peer_ifname")
        state = _first(row, "state", "oper_state", "status", default="").lower()
        if not src or not dst:
            continue
        up = 1 if state in {"up", "active", "ok", "forwarding"} else 0
        netq_link_up.labels(
            cluster=CLUSTER_NAME, src=src, src_port=src_port,
            dst=dst, dst_port=dst_port,
        ).set(up)


def _delta(prev: float, cur: float) -> float:
    if cur is None or prev is None or cur < prev:
        return 0.0
    return float(cur - prev)


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def poll_interfaces(client: NetQClient) -> None:
    payload = client.get(INTERFACES_PATH)
    rows = list(_as_iterable(payload))
    log.info("interfaces: %d rows", len(rows))
    netq_interface_oper_up.clear()
    for row in rows:
        hostname = _first(row, "hostname", "device", "name")
        port = _first(row, "ifname", "interface", "port", "if_name")
        if not hostname or not port:
            continue
        oper = _first(row, "oper_state", "state", "status", default="").lower()
        netq_interface_oper_up.labels(
            cluster=CLUSTER_NAME, hostname=hostname, port=port
        ).set(1 if oper in {"up", "forwarding", "ok"} else 0)
        bin_ = _to_float(row.get("in_bytes") or row.get("rx_bytes"))
        bout = _to_float(row.get("out_bytes") or row.get("tx_bytes"))
        key = (hostname, port)
        prev_in, prev_out = _byte_state.get(key, (None, None))
        for cur, prev, counter in (
            (bin_, prev_in, netq_interface_bytes_in),
            (bout, prev_out, netq_interface_bytes_out),
        ):
            if cur is None:
                continue
            delta = _delta(prev if prev is not None else cur, cur)
            if delta > 0:
                counter.labels(cluster=CLUSTER_NAME, hostname=hostname, port=port).inc(delta)
        _byte_state[key] = (
            bin_ if bin_ is not None else prev_in,
            bout if bout is not None else prev_out,
        )


# Poll loop
_last_success_ts: float = 0.0
_last_success_lock = threading.Lock()


def _mark_success() -> None:
    global _last_success_ts
    with _last_success_lock:
        _last_success_ts = time.time()


def poll_once(client: NetQClient) -> bool:
    start = time.time()
    any_error = False
    pollers = (
        ("devices", poll_devices, DEVICES_PATH),
        ("events", poll_events, EVENTS_PATH),
        ("links", poll_links, LINKS_PATH),
        ("interfaces", poll_interfaces, INTERFACES_PATH),
    )
    for name, fn, path in pollers:
        try:
            fn(client)
        except Exception as exc:  # noqa: BLE001 -- exporter must keep running
            any_error = True
            log.error("poll %s failed: %s", name, exc)
            netq_scrape_errors_total.labels(cluster=CLUSTER_NAME, endpoint=path).inc()
    netq_up.labels(cluster=CLUSTER_NAME).set(0 if any_error else 1)
    netq_scrape_duration_seconds.labels(cluster=CLUSTER_NAME).set(time.time() - start)
    if not any_error:
        _mark_success()
    return not any_error


def poll_loop(client: NetQClient, stop_evt: threading.Event) -> None:
    netq_up.labels(cluster=CLUSTER_NAME).set(0)  # seed before first poll
    while not stop_evt.is_set():
        try:
            poll_once(client)
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected poll error: %s", exc)
        stop_evt.wait(SCRAPE_INTERVAL)


# Health endpoint
class HealthHandler(BaseHTTPRequestHandler):
    server_version = "nvis-netq-exporter/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return  # silence default access logging

    def _respond(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path in ("/healthz", "/-/healthy"):
            with _last_success_lock:
                age = time.time() - _last_success_ts if _last_success_ts else 1e9
            healthy = age < (SCRAPE_INTERVAL * 4)
            self._respond(200 if healthy else 503,
                          "text/plain; charset=utf-8",
                          b"ok" if healthy else b"degraded")
            return
        if path == "/metrics":
            self._respond(200, "text/plain; version=0.0.4", generate_latest(REGISTRY))
            return
        self._respond(404, "text/plain", b"not found")


def run_http_server(port: int, stop_evt: threading.Event) -> None:
    srv = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("listening on :%d", port)

    def _shutdown(*_: Any) -> None:
        log.info("shutdown requested")
        stop_evt.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    srv.serve_forever()


# Main
def main() -> int:
    if not NETQ_URL:
        log.error("NETQ_ENDPOINT is required (base NetQ API URL)")
        return 2
    if not NETQ_TOKEN and not (NETQ_USER and NETQ_PASS):
        log.error("either NETQ_TOKEN or NETQ_USER+NETQ_PASS must be set")
        return 2

    client = NetQClient()
    stop_evt = threading.Event()

    poller = threading.Thread(
        target=poll_loop, args=(client, stop_evt), name="netq-poller", daemon=True)
    poller.start()
    try:
        run_http_server(PORT, stop_evt)
    except KeyboardInterrupt:
        log.info("interrupted")
    stop_evt.set()
    poller.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
