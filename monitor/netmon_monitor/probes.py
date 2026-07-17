"""Measurement probes — stdlib only (subprocess ping, sockets, urllib).

Cross-platform: Linux and macOS shell out to `ping`; Windows uses the
IcmpSendEcho API via ctypes (locale-independent, exact RTT — parsing the
localized `ping.exe` output would be fragile) with a subprocess fallback.
"""

from __future__ import annotations

import ipaddress
import platform
import re
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request

_SYSTEM = platform.system()  # 'Linux' | 'Darwin' | 'Windows'
_RTT_RE_UNIX = re.compile(r"time=([0-9.]+) ms")
_RTT_RE_WIN = re.compile(r"[=<]([0-9]+)\s*ms")  # matches localized time=13ms / čas<1ms
_WIN_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flash from a service

_GATEWAY_CMDS = {
    "Linux": ["ip", "route", "show", "default"],
    "Darwin": ["route", "-n", "get", "default"],
    "Windows": ["route", "print", "0.0.0.0"],
}


def _parse_gateway(out: str, system: str) -> str | None:
    if system == "Darwin":
        for line in out.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "gateway":
                return value.strip() or None
        return None
    if system == "Windows":
        # the persistent/active route tables print "0.0.0.0  0.0.0.0  <gw> ..."
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] == "0.0.0.0" and fields[1] == "0.0.0.0":
                try:
                    return str(ipaddress.ip_address(fields[2]))
                except ValueError:
                    continue
        return None
    fields = out.split()
    if "via" in fields:
        return fields[fields.index("via") + 1]
    return None


def detect_gateway(fallback: str | None = None, system: str = _SYSTEM) -> str | None:
    """Default gateway IP from the OS routing table (survives network changes)."""
    try:
        out = subprocess.run(
            _GATEWAY_CMDS.get(system, _GATEWAY_CMDS["Linux"]),
            capture_output=True, text=True, timeout=5,
            creationflags=_WIN_NO_WINDOW if system == "Windows" else 0,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    return _parse_gateway(out, system) or fallback


def _ping_cmd(ip: str, timeout: float, system: str) -> list[str]:
    if system == "Windows":
        return ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    if system == "Darwin":
        return ["ping", "-n", "-c", "1", "-W", str(int(timeout * 1000)), ip]
    return ["ping", "-n", "-c", "1", "-W", str(int(timeout)), ip]


def _parse_rtt(out: str, system: str) -> float | None:
    m = (_RTT_RE_WIN if system == "Windows" else _RTT_RE_UNIX).search(out)
    return float(m.group(1)) if m else None


# set on the first IcmpSendEcho failure: from then on this process pings via
# subprocess ping.exe instead of retrying a crashing API every round
_win_icmp_broken = False


def _ping_windows_icmp(ip: str, timeout: float):
    """IPv4 ping via iphlpapi.IcmpSendEcho.

    Returns ("ok", rtt_ms), ("LOSS", None), or None when the API is
    unavailable (caller falls back to subprocess ping). Prototypes must be
    declared explicitly — the ctypes defaults truncate the 64-bit HANDLE
    to int, which crashes with an access violation.
    """
    global _win_icmp_broken
    if _win_icmp_broken:
        return None
    import ctypes
    import ctypes.wintypes as wt

    class IP_OPTION_INFORMATION(ctypes.Structure):
        _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte),
                    ("Flags", ctypes.c_ubyte), ("OptionsSize", ctypes.c_ubyte),
                    ("OptionsData", ctypes.c_void_p)]

    class ICMP_ECHO_REPLY(ctypes.Structure):
        _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong),
                    ("RoundTripTime", ctypes.c_ulong), ("DataSize", ctypes.c_ushort),
                    ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                    ("Options", IP_OPTION_INFORMATION)]

    INVALID_HANDLE = wt.HANDLE(-1).value
    try:
        iphlpapi = ctypes.windll.iphlpapi
        create = iphlpapi.IcmpCreateFile
        create.restype = wt.HANDLE
        create.argtypes = []
        close = iphlpapi.IcmpCloseHandle
        close.restype = wt.BOOL
        close.argtypes = [wt.HANDLE]
        send = iphlpapi.IcmpSendEcho
        send.restype = wt.DWORD
        send.argtypes = [wt.HANDLE, ctypes.c_uint32, ctypes.c_char_p, wt.WORD,
                         ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD]

        addr = int.from_bytes(socket.inet_aton(ip), "little")
        handle = create()
        if not handle or handle == INVALID_HANDLE:
            _win_icmp_broken = True
            return None
        try:
            payload = b"netmon-ping"
            buf_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8
            buf = ctypes.create_string_buffer(buf_size)
            n = send(handle, addr, payload, len(payload), None,
                     buf, buf_size, int(timeout * 1000))
            if n == 0:
                return "LOSS", None
            reply = ctypes.cast(buf, ctypes.POINTER(ICMP_ECHO_REPLY)).contents
            if reply.Status != 0:  # 0 = IP_SUCCESS; anything else = no usable reply
                return "LOSS", None
            return "ok", float(reply.RoundTripTime)
        finally:
            close(handle)
    except (AttributeError, OSError, ValueError):
        # windll missing (non-Windows), API crash, bad address — never let the
        # ping loop die; the subprocess fallback takes over permanently
        _win_icmp_broken = True
        return None


def ping_target(ip: str, timeout: float, system: str = _SYSTEM) -> tuple[str, float | None]:
    """Single ping. Returns ("ok", rtt_ms) or ("LOSS", None)."""
    if system == "Windows":
        result = _ping_windows_icmp(ip, timeout)
        if result is not None:
            return result
    try:
        res = subprocess.run(
            _ping_cmd(ip, timeout, system),
            capture_output=True, text=True, timeout=timeout + 3,
            creationflags=_WIN_NO_WINDOW if system == "Windows" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "LOSS", None
    ok = res.returncode == 0
    if system == "Windows":
        # "Destination host unreachable" exits 0 but has no TTL= in the reply
        ok = ok and "TTL=" in res.stdout.upper()
    if not ok:
        return "LOSS", None
    return "ok", _parse_rtt(res.stdout, system)


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


def public_ip(url: str, timeout: float = 10.0) -> str | None:
    """The public IP as seen from outside (plain-text echo service).

    Returns a normalized IP string, or None on any failure — the caller
    records nothing in that case (an unknown IP is not a change).
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netmon/2"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(64).decode("ascii", "replace").strip()
        return str(ipaddress.ip_address(text))
    except (OSError, ValueError):
        return None


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
