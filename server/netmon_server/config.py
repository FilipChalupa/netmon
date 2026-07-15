"""Konfigurace serveru: env proměnné + monitors.toml (seznam monitorů)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:  # Python 3.10 (lokální vývoj) — v Dockeru je 3.12
    import tomli as tomllib


@dataclass
class MonitorCfg:
    name: str
    url: str
    label: str = ""
    token: str = ""

    def __post_init__(self):
        self.url = self.url.rstrip("/")
        if not self.label:
            self.label = self.name


@dataclass
class ServerConfig:
    db_path: str = "data/netmon.db"
    monitors_path: str = "config/monitors.toml"
    tz: str = "Europe/Prague"
    report_hour: int = 3
    sync_interval: float = 30.0
    ping_interval: float = 2.0   # pro odvození výpadků; přepíše /api/info monitoru
    monitors: list[MonitorCfg] = field(default_factory=list)


def load_config() -> ServerConfig:
    cfg = ServerConfig(
        db_path=os.environ.get("NETMON_DB", ServerConfig.db_path),
        monitors_path=os.environ.get("NETMON_MONITORS", ServerConfig.monitors_path),
        tz=os.environ.get("NETMON_TZ", ServerConfig.tz),
        report_hour=int(os.environ.get("NETMON_REPORT_HOUR", ServerConfig.report_hour)),
    )
    if os.path.exists(cfg.monitors_path):
        with open(cfg.monitors_path, "rb") as f:
            data = tomllib.load(f)
        cfg.sync_interval = float(data.get("sync_interval", cfg.sync_interval))
        for m in data.get("monitors", []):
            cfg.monitors.append(MonitorCfg(
                name=m["name"],
                url=m["url"],
                label=m.get("label", ""),
                token=m.get("token", ""),
            ))
    return cfg
