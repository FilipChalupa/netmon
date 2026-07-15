"""Server configuration: environment variables + monitors.toml (monitor list)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    import tomllib
except ImportError:  # Python 3.10 (local development) — Docker runs 3.12
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
    ping_interval: float = 2.0   # for outage derivation; monitor's /api/info overrides
    monitors: list[MonitorCfg] = field(default_factory=list)

    # token for the /import upload page; empty = uploads disabled
    import_token: str = ""

    # alerting (see alerts.py); requires SMTP_* to actually send anything
    alerts_enabled: bool = True
    alert_min_outage_s: int = 60      # alert on outages at least this long
    alert_offline_s: int = 600        # alert when a monitor hasn't synced this long
    alert_interval: float = 60.0      # how often the alert loop checks
    alert_lookback_s: int = 7200      # how far back to look for outage events


def load_config() -> ServerConfig:
    cfg = ServerConfig(
        db_path=os.environ.get("NETMON_DB", ServerConfig.db_path),
        monitors_path=os.environ.get("NETMON_MONITORS", ServerConfig.monitors_path),
        tz=os.environ.get("NETMON_TZ", ServerConfig.tz),
        report_hour=int(os.environ.get("NETMON_REPORT_HOUR", ServerConfig.report_hour)),
        import_token=os.environ.get("NETMON_IMPORT_TOKEN", ""),
        alerts_enabled=os.environ.get("NETMON_ALERTS", "1") != "0",
        alert_min_outage_s=int(os.environ.get("NETMON_ALERT_MIN_OUTAGE_S",
                                              ServerConfig.alert_min_outage_s)),
        alert_offline_s=int(os.environ.get("NETMON_ALERT_OFFLINE_S",
                                           ServerConfig.alert_offline_s)),
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
