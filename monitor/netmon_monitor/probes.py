"""Measurement probes — stdlib only (subprocess ping, sockets, urllib)."""

from __future__ import annotations

import re
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request

_RTT_RE = re.compile(r"time=([0-9.]+) ms")


def detect_gateway(fallback: str | None = None) -> str | None:
    """Default gateway IP from `ip route show default` (survives network changes)."""
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    fields = out.split()
    if "via" in fields:
        return fields[fields.index("via") + 1]
    return fallback


def ping_target(ip: str, timeout: float) -> tuple[str, float | None]:
    """Single ping. Returns ("ok", rtt_ms) or ("LOSS", None)."""
    try:
        res = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", str(int(timeout)), ip],
            capture_output=True, text=True, timeout=timeout + 3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "LOSS", None
    if res.returncode != 0:
        return "LOSS", None
    m = _RTT_RE.search(res.stdout)
    return "ok", float(m.group(1)) if m else None


def reach_probe(url: str, total_timeout: float = 10.0):
    """Time the DNS resolve / TCP connect / TLS handshake phases + HTTP status.

    Returns (dns_ms, tcp_ms, tls_ms, http_code, status) — on failure
    (None, None, None, 0, "FAIL"), matching the FAIL rows of the bash version.
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    use_tls = parsed.scheme == "https"
    port = parsed.port or (443 if use_tls else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    deadline = time.monotonic() + total_timeout
    sock = None
    try:
        t0 = time.monotonic()
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        dns_ms = (time.monotonic() - t0) * 1000
        addr = infos[0][4]

        t1 = time.monotonic()
        sock = socket.create_connection(addr[:2], timeout=max(deadline - t1, 0.1))
        tcp_ms = (time.monotonic() - t1) * 1000

        tls_ms = 0.0
        if use_tls:
            t2 = time.monotonic()
            ctx = ssl.create_default_context()
            sock.settimeout(max(deadline - t2, 0.1))
            sock = ctx.wrap_socket(sock, server_hostname=host)
            tls_ms = (time.monotonic() - t2) * 1000

        sock.settimeout(max(deadline - time.monotonic(), 0.1))
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            f"User-Agent: netmon/2\r\nConnection: close\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n" not in buf and len(buf) < 4096:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buf += chunk
        status_line = buf.split(b"\r\n", 1)[0].split()
        code = int(status_line[1]) if len(status_line) >= 2 else 0
        if code == 0:
            return None, None, None, 0, "FAIL"
        return round(dns_ms, 1), round(tcp_ms, 1), round(tls_ms, 1), code, "ok"
    except (OSError, ssl.SSLError, ValueError, IndexError):
        return None, None, None, 0, "FAIL"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def adaptive_speed_bytes(measured_mbps: float, min_seconds: float,
                         max_bytes: int) -> int:
    """Payload size for the second, more accurate speed measurement.

    Aims for a download lasting ~2× min_seconds at the measured speed so TCP
    has time to ramp up; rounded to whole MB and capped at max_bytes.
    """
    target_seconds = min_seconds * 2
    size = int(measured_mbps * 1_000_000 / 8 * target_seconds)
    size = (size // 1_000_000) * 1_000_000
    return min(size, max_bytes)


def speed_test(url: str, max_time: float = 120.0, stop=None):
    """Download a test file and measure throughput.

    Returns (down_mbps, bytes, seconds, http_code) — on failure
    (None, None, seconds|None, 0). `stop` (threading.Event) aborts the
    download between chunks so service shutdown never waits for a full test.
    """
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netmon/2"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = resp.status
            total = 0
            while True:
                if stop is not None and stop.is_set():
                    return None, None, round(time.monotonic() - start, 3), 0
                if time.monotonic() - start > max_time:
                    return None, None, round(time.monotonic() - start, 3), 0
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
        seconds = time.monotonic() - start
        if code != 200 or total == 0 or seconds <= 0:
            return None, None, round(seconds, 3), code
        mbps = total * 8 / 1_000_000 / seconds
        return round(mbps, 2), total, round(seconds, 6), code
    except OSError:
        return None, None, round(time.monotonic() - start, 3), 0
