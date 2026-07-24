"""Measurement loop threads. All loops share a single stop Event."""

from __future__ import annotations

import concurrent.futures
import datetime
import random
import threading
import time

from .config import Config
from .db import Db
from . import probes

GATEWAY_REDETECT_INTERVAL = 60.0


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class TargetSet:
    """Ping targets with automatic gateway re-detection (target with IP 'auto')."""

    def __init__(self, cfg: Config):
        self._spec = cfg.targets
        self._gateway_ip = None
        self._detected_at = 0.0

    def resolve(self) -> list[tuple[str, str]]:
        out = []
        for name, ip in self._spec:
            if ip == "auto":
                now = time.monotonic()
                if self._gateway_ip is None or now - self._detected_at > GATEWAY_REDETECT_INTERVAL:
                    detected = probes.detect_gateway(fallback=self._gateway_ip)
                    if detected:
                        self._gateway_ip = detected
                    self._detected_at = now
                if self._gateway_ip is None:
                    continue  # gateway unknown yet — skip this target this round
                out.append((name, self._gateway_ip))
            else:
                out.append((name, ip))
        return out


def ping_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    targets = TargetSet(cfg)
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(len(cfg.targets), 1), thread_name_prefix="ping"
    )
    next_t = time.monotonic()
    try:
        while not stop.is_set():
            resolved = targets.resolve()
            ts_iso = now_iso()
            ts_epoch = time.time()
            futures = {
                name: (ip, pool.submit(probes.ping_target, ip, cfg.ping_timeout))
                for name, ip in resolved
            }
            for name, (ip, fut) in futures.items():
                status, rtt = fut.result()
                db.insert_latency(ts_epoch, ts_iso, name, ip, status, rtt)

            next_t += cfg.ping_interval
            delay = next_t - time.monotonic()
            if delay < 0:
                next_t = time.monotonic()  # round overran — don't try to catch up
                delay = 0
            stop.wait(delay)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def reach_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    while not stop.is_set():  # first probe right at startup (baseline), like bash
        dns_ms, tcp_ms, tls_ms, code, status = probes.reach_probe(cfg.reach_url)
        db.insert_reach(time.time(), now_iso(), dns_ms, tcp_ms, tls_ms, code, status)
        stop.wait(cfg.reach_interval)


def measure_download(cfg: Config, stop: threading.Event):
    """One download measurement, with an adaptive second pass on fast lines:
    a test finishing under speed_min_seconds underestimates (TCP ramp-up),
    so it is repeated once with a payload sized for ~2× that duration."""
    mbps, bytes_, seconds, code = probes.speed_test(cfg.resolved_speed_url(), stop=stop)
    if (mbps is not None and seconds is not None
            and seconds < cfg.speed_min_seconds and not stop.is_set()):
        size2 = probes.adaptive_speed_bytes(mbps, cfg.speed_min_seconds,
                                            cfg.speed_max_bytes)
        if size2 > cfg.speed_bytes:
            m2, b2, s2, c2 = probes.speed_test(
                cfg.speed_url.format(bytes=size2), stop=stop)
            if m2 is not None:  # keep the first result if the retry failed
                return m2, b2, s2, c2
    return mbps, bytes_, seconds, code


def measure_upload(cfg: Config, stop: threading.Event):
    """One upload measurement with the same adaptive second pass as download."""
    up, ub, us, uc = probes.upload_test(cfg.upload_url, cfg.upload_bytes, stop=stop)
    if (up is not None and us is not None
            and us < cfg.speed_min_seconds and not stop.is_set()):
        size2 = probes.adaptive_speed_bytes(up, cfg.speed_min_seconds,
                                            cfg.upload_max_bytes)
        if size2 > cfg.upload_bytes:
            u2, b2, s2, c2 = probes.upload_test(cfg.upload_url, size2, stop=stop)
            if u2 is not None:
                return u2, b2, s2, c2
    return up, ub, us, uc


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _bloat_target_ip(cfg: Config) -> str | None:
    """First public ping target — bufferbloat is about the internet path,
    the gateway would only show the LAN hop."""
    return next((ip for _, ip in cfg.targets if ip != "auto"), None)


def _idle_rtt(ip: str, timeout: float, stop: threading.Event,
              samples: int = 5) -> float | None:
    rtts = []
    for _ in range(samples):
        if stop.is_set():
            break
        status, rtt = probes.ping_target(ip, timeout)
        if rtt is not None:
            rtts.append(rtt)
    return _median(rtts)


class _LoadPinger(threading.Thread):
    """Pings `ip` in the background while a speed transfer saturates the
    line; finish() returns the median RTT under load."""

    def __init__(self, ip: str, timeout: float, stop: threading.Event):
        super().__init__(name="bloat-ping", daemon=True)
        self.ip = ip
        self.timeout = timeout
        self.stop = stop
        self.rtts: list[float] = []
        self._done = threading.Event()

    def run(self) -> None:
        while not self._done.is_set() and not self.stop.is_set():
            status, rtt = probes.ping_target(self.ip, self.timeout)
            if rtt is not None:
                self.rtts.append(rtt)
            self._done.wait(0.3)

    def finish(self) -> float | None:
        self._done.set()
        self.join(timeout=self.timeout + 5)
        return _median(self.rtts)


def measure_speed(cfg: Config, stop: threading.Event):
    """Download + upload in one round, with latency measured before and
    during the transfers (bufferbloat). Returns (down_mbps, bytes, seconds,
    http_code, up_mbps, idle_rtt_ms, loaded_rtt_ms); bytes/seconds/http_code
    describe the download leg, the rest are None when disabled/failed."""
    ip = _bloat_target_ip(cfg)
    idle = loaded = None
    pinger = None
    if ip is not None and not stop.is_set():
        idle = _idle_rtt(ip, cfg.ping_timeout, stop)
        pinger = _LoadPinger(ip, cfg.ping_timeout, stop)
        pinger.start()
    try:
        mbps, bytes_, seconds, code = measure_download(cfg, stop)
        up_mbps = None
        if cfg.upload_url and cfg.upload_bytes > 0 and not stop.is_set():
            up_mbps = measure_upload(cfg, stop)[0]
    finally:
        if pinger is not None:
            loaded = pinger.finish()
    if mbps is None and up_mbps is None:
        idle = loaded = None  # nothing saturated the line — no load to measure
    return mbps, bytes_, seconds, code, up_mbps, idle, loaded


def speed_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    while not stop.is_set():  # first test right at startup, then hourly
        mbps, bytes_, seconds, code, up_mbps, idle, loaded = measure_speed(cfg, stop)
        if not stop.is_set() or mbps is not None:
            db.insert_speed(time.time(), now_iso(), mbps, bytes_, seconds, code,
                            up_mbps, idle, loaded)
        # ±10 % jitter (max ±5 min): monitors that booted together (power
        # outage) would otherwise saturate a shared uplink at the same moment
        # forever; drifting the cadence apart costs nothing
        jitter = min(cfg.speed_interval * 0.1, 300.0)
        stop.wait(cfg.speed_interval + random.uniform(-jitter, jitter))


def heartbeat_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    while not stop.wait(cfg.heartbeat_interval):
        db.insert_uptime(time.time(), now_iso(), "ALIVE")


def internet_outage_active(rows, gateway_names: set[str],
                           public_names: list[str], min_rounds: int) -> bool:
    """True when the last min_rounds complete ping rounds each lost ALL public
    targets while the gateway still answered (= internet outage, not local).

    rows: (ts_epoch, target, status) tuples ordered by time.
    """
    rounds: dict[float, dict[str, str]] = {}
    for ts, target, status in rows:
        rounds.setdefault(ts, {})[target] = status
    last = sorted(rounds)[-min_rounds:]
    if len(last) < min_rounds:
        return False
    for ts in last:
        rd = rounds[ts]
        if not public_names or not all(rd.get(t) == "LOSS" for t in public_names):
            return False
        if any(rd.get(g) == "LOSS" for g in gateway_names):
            return False  # gateway down too → local problem, route is moot
    return True


def diag_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    """Capture one traceroute per public target while the internet is down —
    the broken route can't be reconstructed after the fact. Re-arms after
    diag_cooldown so a flapping line doesn't run traceroutes back to back."""
    if not cfg.diag_enabled:
        return
    gateway_names = {n for n, ip in cfg.targets if ip == "auto"}
    publics = [(n, ip) for n, ip in cfg.targets if ip != "auto"]
    window = max(cfg.ping_interval * (cfg.diag_min_rounds + 1.5), 10.0)
    last_fire = -1e12
    while not stop.wait(5.0):
        if time.monotonic() - last_fire < cfg.diag_cooldown:
            continue
        rows = db.recent_latency(time.time() - window)
        if not internet_outage_active(rows, gateway_names,
                                      [n for n, _ in publics], cfg.diag_min_rounds):
            continue
        last_fire = time.monotonic()
        for name, ip in publics:
            if stop.is_set():
                break
            out = probes.traceroute(ip)
            if out:
                db.insert_diag(time.time(), now_iso(), name, out)


def pubip_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    """Record the public IP only when it changes (ISP handover, CGNAT,
    reconnect). last_pubip() seeds the comparison so restarts don't
    duplicate; a failed check records nothing."""
    last = db.last_pubip()
    while not stop.is_set():  # first check right at startup
        ip = probes.public_ip(cfg.pubip_url)
        if ip and ip != last:
            db.insert_pubip(time.time(), now_iso(), ip)
            last = ip
        stop.wait(cfg.pubip_interval)


def purge_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    while not stop.wait(86400):
        db.purge(cfg.retention_days)


ALL_LOOPS = [ping_loop, reach_loop, speed_loop, heartbeat_loop, pubip_loop,
             diag_loop, purge_loop]


def start_workers(cfg: Config, db: Db, stop: threading.Event) -> list[threading.Thread]:
    threads = []
    for fn in ALL_LOOPS:
        t = threading.Thread(target=fn, args=(cfg, db, stop), name=fn.__name__)
        t.start()
        threads.append(t)
    return threads
