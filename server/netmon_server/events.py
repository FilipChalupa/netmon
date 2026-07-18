"""Outage derivation from latency data — port of the legacy/events.sh algorithm.

Classification of a ping round (all targets share ts_epoch):
  - local:    gateway LOSS → problem on the measured network's side
              (cable/switch/router)
  - internet: gateway OK but ALL public targets LOSS → provider problem
  - otherwise: fine (a single public target lost = noise, no event)

Consecutive bad rounds with the same scope merge into an interval; a scope
change splits it. Instead of "adjacent line in the file" (bash), continuity
is judged by a gap ≤ 2.5× ping_interval — which also correctly splits outages
interrupted by periods when the monitor wasn't running.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

GATEWAY_TARGET = "gateway"
PUBLIC_TARGETS = ("quad9", "google")

NOTES = {
    "local": "local link (gateway unreachable)",
    "internet": "internet (both public targets unreachable)",
    "reach": "internet unusable (reach probes failing, pings OK)",
}

# reach samples arrive every ~30 s; a bigger hole means the monitor was down
REACH_GAP_S = 150.0


@dataclass
class Event:
    start_epoch: float
    end_epoch: float
    start_iso: str
    end_iso: str
    duration_s: int
    scope: str  # "local" | "internet"

    @property
    def note(self) -> str:
        return NOTES[self.scope]

    def as_dict(self) -> dict:
        return {
            "start": self.start_iso,
            "end": self.end_iso,
            "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
            "dur": self.duration_s,
            "scope": self.scope,
            "note": self.note,
        }


def derive_events(conn: sqlite3.Connection, network_id: int,
                  t0: float, t1: float, ping_interval: float = 2.0,
                  gateway_target: str = GATEWAY_TARGET,
                  public_targets: tuple[str, ...] = PUBLIC_TARGETS) -> list[Event]:
    rows = conn.execute(
        "SELECT ts_epoch, ts_iso, target FROM latency "
        "WHERE network_id=? AND status='LOSS' AND ts_epoch>=? AND ts_epoch<=? "
        "ORDER BY ts_epoch",
        (network_id, t0, t1),
    ).fetchall()

    # group LOSS rows into rounds by ts_epoch (a round's targets share the timestamp)
    rounds: list[tuple[float, str, set]] = []  # (epoch, iso, {targets with LOSS})
    for r in rows:
        if rounds and rounds[-1][0] == r["ts_epoch"]:
            rounds[-1][2].add(r["target"])
        else:
            rounds.append((r["ts_epoch"], r["ts_iso"], {r["target"]}))

    max_gap = 2.5 * ping_interval
    events: list[Event] = []
    cur: dict | None = None

    def close():
        nonlocal cur
        if cur is not None:
            dur = max(int(round(cur["end"] - cur["start"])), int(ping_interval))
            events.append(Event(cur["start"], cur["end"], cur["start_iso"],
                                cur["end_iso"], dur, cur["scope"]))
            cur = None

    for epoch, iso, lost in rounds:
        if gateway_target in lost:
            scope = "local"
        elif all(t in lost for t in public_targets):
            scope = "internet"
        else:
            close()
            continue
        if cur is not None and cur["scope"] == scope and epoch - cur["end"] <= max_gap:
            cur["end"] = epoch
            cur["end_iso"] = iso
        else:
            close()
            cur = {"start": epoch, "end": epoch, "start_iso": iso,
                   "end_iso": iso, "scope": scope}
    close()
    return events


def derive_reach_events(conn: sqlite3.Connection, network_id: int,
                        t0: float, t1: float, min_fails: int = 10,
                        max_gap_s: float = REACH_GAP_S) -> list[Event]:
    """Runs of consecutive reach-probe FAILs as events — "pings work but the
    internet doesn't" (broken DNS, filtered traffic). Same threshold as the
    email alert; runs are split where the monitor wasn't sampling."""
    rows = conn.execute(
        "SELECT ts_epoch, ts_iso, status FROM reach "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? ORDER BY ts_epoch",
        (network_id, t0, t1),
    ).fetchall()
    events: list[Event] = []
    run: list = []

    def close():
        nonlocal run
        if len(run) >= min_fails:
            start, end = run[0], run[-1]
            dur = max(int(round(end["ts_epoch"] - start["ts_epoch"])), 30)
            events.append(Event(start["ts_epoch"], end["ts_epoch"],
                                start["ts_iso"], end["ts_iso"], dur, "reach"))
        run = []

    for r in rows:
        if r["status"] != "FAIL":
            close()
        elif run and r["ts_epoch"] - run[-1]["ts_epoch"] > max_gap_s:
            close()
            run = [r]
        else:
            run.append(r)
    close()
    return events


def merge_events(ping_events: list[Event],
                 reach_events: list[Event]) -> list[Event]:
    """Ping-derived and reach-derived events on one timeline. A reach run
    overlapping a hard outage is its consequence, not a second incident —
    it is dropped, mirroring the email alert's suppression."""
    kept = [r for r in reach_events
            if not any(e.start_epoch <= r.end_epoch and e.end_epoch >= r.start_epoch
                       for e in ping_events)]
    return sorted(ping_events + kept, key=lambda e: e.start_epoch)


def events_summary(events: list[Event]) -> dict:
    out = {}
    for scope in ("local", "internet", "reach"):
        evs = [e for e in events if e.scope == scope]
        if not evs:
            continue
        longest = max(evs, key=lambda e: e.duration_s)
        out[scope] = {
            "count": len(evs),
            "total_s": sum(e.duration_s for e in evs),
            "longest_s": longest.duration_s,
            "longest_at": longest.start_iso,
        }
    return out
