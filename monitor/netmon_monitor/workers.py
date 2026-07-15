"""Measurement loop threads. All loops share a single stop Event."""

from __future__ import annotations

import concurrent.futures
import datetime
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


def speed_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    while not stop.is_set():  # first test right at startup, then hourly
        mbps, bytes_, seconds, code = probes.speed_test(
            cfg.resolved_speed_url(), stop=stop
        )
        if not stop.is_set() or mbps is not None:
            db.insert_speed(time.time(), now_iso(), mbps, bytes_, seconds, code)
        stop.wait(cfg.speed_interval)


def heartbeat_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    while not stop.wait(cfg.heartbeat_interval):
        db.insert_uptime(time.time(), now_iso(), "ALIVE")


def purge_loop(cfg: Config, db: Db, stop: threading.Event) -> None:
    while not stop.wait(86400):
        db.purge(cfg.retention_days)


ALL_LOOPS = [ping_loop, reach_loop, speed_loop, heartbeat_loop, purge_loop]


def start_workers(cfg: Config, db: Db, stop: threading.Event) -> list[threading.Thread]:
    threads = []
    for fn in ALL_LOOPS:
        t = threading.Thread(target=fn, args=(cfg, db, stop), name=fn.__name__)
        t.start()
        threads.append(t)
    return threads
