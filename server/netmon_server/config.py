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

    # alerting (see alerts.py); requires SMTP_* to actually send anything
    alerts_enabled: bool = True
    alert_min_outage_s: int = 60      # alert on outages at least this long
    alert_offline_s: int = 600        # alert when a monitor hasn't synced this long
    alert_reach_fails: int = 10       # consecutive reach FAILs (~5 min at 30 s) to alert
    alert_interval: float = 60.0      # how often the alert loop checks
    alert_lookback_s: int = 7200      # how far back to look for outage events
    # speed degradation: recent median below this % of the 30-day baseline
    # median → alert; recovery above +20 points (capped 90 %). 0 disables.
    alert_speed_pct: int = 50
    alert_speed_window_s: int = 6 * 3600   # "recent" = tests in this window
    alert_speed_min_tests: int = 3         # need at least this many recent tests
    alert_speed_min_baseline: int = 24     # and this many baseline tests (~1 day)


# single-mode (all-in-one binary) injects its config here instead of env/toml
_override: ServerConfig | None = None


def set_config_override(cfg: ServerConfig | None) -> None:
    global _override
    _override = cfg


def load_config() -> ServerConfig:
    if _override is not None:
        return _override
    cfg = ServerConfig(
        db_path=os.environ.get("NETMON_DB", ServerConfig.db_path),
        monitors_path=os.environ.get("NETMON_MONITORS", ServerConfig.monitors_path),
        tz=os.environ.get("NETMON_TZ", ServerConfig.tz),
        report_hour=int(os.environ.get("NETMON_REPORT_HOUR", ServerConfig.report_hour)),
        alerts_enabled=os.environ.get("NETMON_ALERTS", "1") != "0",
        alert_min_outage_s=int(os.environ.get("NETMON_ALERT_MIN_OUTAGE_S",
                                              ServerConfig.alert_min_outage_s)),
        alert_offline_s=int(os.environ.get("NETMON_ALERT_OFFLINE_S",
                                           ServerConfig.alert_offline_s)),
        alert_reach_fails=int(os.environ.get("NETMON_ALERT_REACH_FAILS",
                                             ServerConfig.alert_reach_fails)),
        alert_speed_pct=int(os.environ.get("NETMON_ALERT_SPEED_PCT",
                                           ServerConfig.alert_speed_pct)),
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
