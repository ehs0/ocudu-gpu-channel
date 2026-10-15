"""Durable, bounded, priority-ordered event queue of one robot's UE link (stdlib sqlite3).

Rules (`MULTI_ROBOT_RESCUE_MILESTONES.md`, offline-first synchronisation):

- Events are stored, not dropped, while the application link is down.
- `next_batch` returns unacknowledged events by priority (0 first), then seq.
  An event whose last send is younger than the retransmission timeout is
  skipped (in flight).
- Coalescing types (pose, map tile) keep only the newest event per key; the
  older one is removed even if it was sent but not yet acknowledged — its ACK,
  if it ever arrives, then matches nothing.
- When the stored bytes exceed `max_bytes`, the lowest-priority, oldest events
  are evicted first; priority 0 (survivor reports) is never evicted. Every
  eviction is counted and reported to the caller.
- Acknowledged events are deleted; their timing (captured, enqueued, first
  send, ack) is returned so the caller can log it.
- The database file survives a process restart (WAL journal).
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from dataclasses import dataclass

NEVER_EVICT_PRIORITY = 0


@dataclass
class Delivered:
    event_id: str
    type: str
    priority: int
    captured_at_ms: int
    enqueued_ms: int
    first_send_ms: int | None
    acked_ms: int
    send_count: int
    size: int


class OfflineQueue:
    def __init__(self, path: pathlib.Path | str, *, max_bytes: int):
        self.max_bytes = int(max_bytes)
        self.evicted = 0
        self.evicted_bytes = 0
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL,
                type TEXT NOT NULL,
                priority INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                coalesce_key TEXT,
                captured_at_ms INTEGER NOT NULL,
                enqueued_ms INTEGER NOT NULL,
                first_send_ms INTEGER,
                last_send_ms INTEGER,
                send_count INTEGER NOT NULL DEFAULT 0,
                size INTEGER NOT NULL,
                body TEXT NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_order ON events(priority, seq)")
        self.db.execute("CREATE INDEX IF NOT EXISTS by_key ON events(coalesce_key)")

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ writes

    def put(self, event: dict, *, priority: int, coalesce_key: str | None, now_ms: int) -> int:
        """Store one event; returns the number of events evicted to stay under max_bytes."""

        body = json.dumps(event, separators=(",", ":"))
        self.db.execute("BEGIN")
        if coalesce_key is not None:
            self.db.execute("DELETE FROM events WHERE coalesce_key = ?", (coalesce_key,))
        self.db.execute(
            "INSERT OR IGNORE INTO events (event_id, mission_id, type, priority, seq, coalesce_key, "
            "captured_at_ms, enqueued_ms, size, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event["event_id"], event["mission_id"], event["type"], priority, event["seq"], coalesce_key,
             event["captured_at_ms"], now_ms, len(body), body))
        evicted = self._evict()
        self.db.execute("COMMIT")
        return evicted

    def _evict(self) -> int:
        evicted = 0
        while self.bytes() > self.max_bytes:
            row = self.db.execute(
                "SELECT event_id, size FROM events WHERE priority > ? ORDER BY priority DESC, seq ASC LIMIT 1",
                (NEVER_EVICT_PRIORITY,)).fetchone()
            if row is None:
                break   # only never-evict events left: keep them over budget
            self.db.execute("DELETE FROM events WHERE event_id = ?", (row[0],))
            evicted += 1
            self.evicted_bytes += row[1]
        self.evicted += evicted
        return evicted

    def mark_sent(self, event_ids: list[str], now_ms: int) -> None:
        self.db.executemany(
            "UPDATE events SET send_count = send_count + 1, last_send_ms = ?, "
            "first_send_ms = COALESCE(first_send_ms, ?) WHERE event_id = ?",
            [(now_ms, now_ms, event_id) for event_id in event_ids])

    def ack(self, event_ids: list[str], now_ms: int) -> list[Delivered]:
        delivered = []
        self.db.execute("BEGIN")
        for event_id in event_ids:
            row = self.db.execute(
                "SELECT type, priority, captured_at_ms, enqueued_ms, first_send_ms, send_count, size "
                "FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if row is None:
                continue   # already acknowledged, or superseded by a newer coalesced event
            self.db.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
            delivered.append(Delivered(event_id, row[0], row[1], row[2], row[3], row[4], now_ms, row[5], row[6]))
        self.db.execute("COMMIT")
        return delivered

    def purge_other_missions(self, mission_id: str) -> int:
        cursor = self.db.execute("DELETE FROM events WHERE mission_id != ?", (mission_id,))
        return cursor.rowcount

    # ------------------------------------------------------------------ reads

    def next_batch(self, *, now_ms: int, rto_ms: int, limit: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT body FROM events WHERE last_send_ms IS NULL OR last_send_ms <= ? "
            "ORDER BY priority ASC, seq ASC LIMIT ?", (now_ms - rto_ms, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def bytes(self) -> int:
        return self.db.execute("SELECT COALESCE(SUM(size), 0) FROM events").fetchone()[0]

    def counts_by_type(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall())

    def in_flight(self, *, now_ms: int, rto_ms: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events WHERE last_send_ms > ?",
                               (now_ms - rto_ms,)).fetchone()[0]
