"""Odvození výpadků z latency dat — port algoritmu z legacy/events.sh.

Klasifikace kola pingů (všechny cíle sdílejí ts_epoch):
  - local:    brána LOSS → problém na straně měřené sítě (kabel/switch/router)
  - internet: brána OK, ale VŠECHNY veřejné cíle LOSS → problém u providera
  - jinak:    v pořádku (ztráta jediného veřejného cíle = šum, žádná událost)

Po sobě jdoucí špatná kola se stejným scope se slučují do intervalu; změna
scope interval rozdělí. Místo "sousední řádek v souboru" (bash) se souvislost
pozná mezerou ≤ 2,5× ping_interval — správně tak rozdělí i výpadky přerušené
dobou, kdy monitor neběžel.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

GATEWAY_TARGET = "gateway"
PUBLIC_TARGETS = ("quad9", "google")

NOTES = {
    "local": "lokální linka (brána nedostupná)",
    "internet": "internet (oba veřejné cíle nedostupné)",
}


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

    # seskupení LOSS řádků do kol podle ts_epoch (cíle kola sdílejí timestamp)
    rounds: list[tuple[float, str, set]] = []  # (epoch, iso, {targets s LOSS})
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


def events_summary(events: list[Event]) -> dict:
    out = {}
    for scope in ("local", "internet"):
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
