"""Configuration loading from an INI file (configparser — works on Py 3.10+)."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # [monitor]
    network: str = "net"
    port: int = 8787
    bind: str = "0.0.0.0"
    token: str = ""
    db_path: str = "~/.local/share/netmon/monitor.db"
    retention_days: int = 90

    # [probes]
    ping_interval: float = 2.0
    ping_timeout: float = 2.0
    # pairs of (name, IP or "auto" = gateway from the default route)
    targets: list[tuple[str, str]] = field(default_factory=lambda: [
        ("gateway", "auto"),
        ("quad9", "9.9.9.9"),
        ("google", "8.8.8.8"),
    ])
    reach_interval: float = 30.0
    reach_url: str = "https://www.google.com/generate_204"
    speed_interval: float = 3600.0
    speed_bytes: int = 50_000_000
    speed_url: str = "https://speed.cloudflare.com/__down?bytes={bytes}"
    # a test finishing faster than this underestimates fast lines (TCP ramp-up)
    # → re-measure once with a larger payload, capped at speed_max_bytes
    speed_min_seconds: float = 3.0
    speed_max_bytes: int = 200_000_000
    heartbeat_interval: float = 60.0
    # public IP is checked often but recorded only when it changes
    pubip_interval: float = 900.0
    pubip_url: str = "https://api.ipify.org"
    # traceroute snapshot while an internet outage is happening
    diag_enabled: bool = True
    diag_min_rounds: int = 3      # rounds of full public loss before firing
    diag_cooldown: float = 600.0  # min seconds between snapshots

    def resolved_db_path(self) -> str:
        return os.path.expanduser(self.db_path)

    def resolved_speed_url(self) -> str:
        return self.speed_url.format(bytes=self.speed_bytes)


def parse_targets(raw: str) -> list[tuple[str, str]]:
    """"gateway=auto, quad9=9.9.9.9" → [("gateway", "auto"), ("quad9", "9.9.9.9")]"""
    out: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, ip = part.partition("=")
        if not name or not ip:
            raise ValueError(f"Invalid ping target: {part!r} (expected 'name=IP')")
        out.append((name.strip(), ip.strip()))
    if not out:
        raise ValueError("Empty ping target list")
    return out


def load_config(path: str) -> Config:
    cp = configparser.ConfigParser()
    read = cp.read(os.path.expanduser(path))
    if not read:
        raise FileNotFoundError(f"Config file not found: {path}")

    cfg = Config()
    m = cp["monitor"] if cp.has_section("monitor") else {}
    cfg.network = m.get("network", cfg.network).strip()
    cfg.port = int(m.get("port", cfg.port))
    cfg.bind = m.get("bind", cfg.bind).strip()
    cfg.token = m.get("token", cfg.token).strip()
    cfg.db_path = m.get("db_path", cfg.db_path).strip()
    cfg.retention_days = int(m.get("retention_days", cfg.retention_days))

    p = cp["probes"] if cp.has_section("probes") else {}
    cfg.ping_interval = float(p.get("ping_interval", cfg.ping_interval))
    cfg.ping_timeout = float(p.get("ping_timeout", cfg.ping_timeout))
    if p.get("targets"):
        cfg.targets = parse_targets(p["targets"])
    cfg.reach_interval = float(p.get("reach_interval", cfg.reach_interval))
    cfg.reach_url = p.get("reach_url", cfg.reach_url).strip()
    cfg.speed_interval = float(p.get("speed_interval", cfg.speed_interval))
    cfg.speed_bytes = int(p.get("speed_bytes", cfg.speed_bytes))
    cfg.speed_url = p.get("speed_url", cfg.speed_url).strip()
    cfg.speed_min_seconds = float(p.get("speed_min_seconds", cfg.speed_min_seconds))
    cfg.speed_max_bytes = int(p.get("speed_max_bytes", cfg.speed_max_bytes))
    cfg.heartbeat_interval = float(p.get("heartbeat_interval", cfg.heartbeat_interval))
    cfg.pubip_interval = float(p.get("pubip_interval", cfg.pubip_interval))
    cfg.pubip_url = p.get("pubip_url", cfg.pubip_url).strip()
    cfg.diag_enabled = str(p.get("diag_enabled", cfg.diag_enabled)).strip().lower() \
        not in ("0", "false", "no")
    cfg.diag_min_rounds = int(p.get("diag_min_rounds", cfg.diag_min_rounds))
    cfg.diag_cooldown = float(p.get("diag_cooldown", cfg.diag_cooldown))
    return cfg
