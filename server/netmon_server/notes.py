"""User notes pinned to a point in time, optionally scoped to networks.

A note with no linked networks is general: it applies to every network and
shows up on all charts. Scoped notes show up only for their networks.
"""

from __future__ import annotations

import sqlite3
import time


def _note_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    nets = conn.execute(
        "SELECT w.name, w.label FROM note_networks nn "
        "JOIN networks w ON w.id = nn.network_id "
        "WHERE nn.note_id=? ORDER BY w.name", (row["id"],)).fetchall()
    return {
        "id": row["id"],
        "ts_epoch": row["ts_epoch"],
        "text": row["text"],
        "created_at": row["created_at"],
        "networks": [{"name": n["name"], "label": n["label"]} for n in nets],
    }


def create_note(conn: sqlite3.Connection, ts_epoch: float, text: str,
                network_names: list[str] | None = None) -> dict:
    """Insert a note; empty network_names makes it general (all networks)."""
    text = text.strip()
    if not text:
        raise ValueError("Note text must not be empty")
    net_ids = []
    for name in network_names or []:
        row = conn.execute("SELECT id FROM networks WHERE name=?", (name,)).fetchone()
        if not row:
            raise ValueError(f"Unknown network: {name}")
        net_ids.append(row["id"])
    cur = conn.execute("INSERT INTO notes(ts_epoch, text, created_at) VALUES(?,?,?)",
                       (ts_epoch, text, time.time()))
    note_id = cur.lastrowid
    conn.executemany("INSERT OR IGNORE INTO note_networks(note_id, network_id) VALUES(?,?)",
                     [(note_id, i) for i in net_ids])
    conn.commit()
    row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return _note_dict(conn, row)


def list_notes(conn: sqlite3.Connection, t0: float, t1: float,
               network_names: list[str] | None = None) -> list[dict]:
    """Notes in [t0, t1]; with network_names, only general notes and notes
    scoped to at least one of those networks."""
    rows = conn.execute("SELECT * FROM notes WHERE ts_epoch >= ? AND ts_epoch <= ? "
                        "ORDER BY ts_epoch", (t0, t1)).fetchall()
    out = [_note_dict(conn, r) for r in rows]
    if network_names is not None:
        wanted = set(network_names)
        out = [n for n in out
               if not n["networks"] or wanted & {w["name"] for w in n["networks"]}]
    return out


def delete_note(conn: sqlite3.Connection, note_id: int) -> bool:
    cur = conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
    conn.commit()
    return cur.rowcount > 0
