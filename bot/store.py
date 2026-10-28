"""
store.py - Append-only events.jsonl and SQLite storage (WP6).
Survives process crashes with per-event flushing and SQLite in WAL mode.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, out_dir: str | Path, meeting_id: str = "unknown"):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.meeting_id = meeting_id
        self.jsonl_path = self.out_dir / "events.jsonl"
        self.db_path = self.out_dir / "session.db"
        self.seq = 0

        # Open jsonl file handle
        self._jsonl_file = open(self.jsonl_path, "a", encoding="utf-8")

        # Initialize SQLite database
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_db()

        # Determine starting seq from DB or JSONL
        cur = self._conn.cursor()
        cur.execute("SELECT COALESCE(MAX(seq), 0) FROM events")
        row = cur.fetchone()
        if row and row[0]:
            self.seq = row[0]

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                type TEXT NOT NULL,
                meeting_id TEXT,
                participant_id TEXT,
                person_key TEXT,
                name TEXT,
                source TEXT,
                confidence TEXT,
                payload TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                seq INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                total INTEGER NOT NULL,
                unique_people INTEGER NOT NULL,
                connections INTEGER NOT NULL,
                cam_on INTEGER NOT NULL,
                cam_off INTEGER NOT NULL,
                cam_unknown INTEGER NOT NULL,
                mic_on INTEGER NOT NULL,
                mic_off INTEGER NOT NULL,
                mic_unknown INTEGER NOT NULL,
                desktop_count INTEGER NOT NULL,
                mobile_count INTEGER NOT NULL,
                dialin_count INTEGER NOT NULL,
                unknown_device_count INTEGER NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                participant_id TEXT PRIMARY KEY,
                person_key TEXT NOT NULL,
                name TEXT NOT NULL,
                device TEXT NOT NULL,
                device_basis TEXT,
                device_confidence TEXT,
                cam TEXT NOT NULL,
                mic TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                is_present INTEGER NOT NULL DEFAULT 1
            )
        """)
        self._conn.commit()

    def record_event(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        """Record an event to both events.jsonl and SQLite atomically."""
        if "seq" not in ev:
            self.seq += 1
            ev["seq"] = self.seq
        else:
            self.seq = max(self.seq, int(ev["seq"]))

        if "ts" not in ev:
            ev["ts"] = iso_now()
        if "meeting_id" not in ev and self.meeting_id:
            ev["meeting_id"] = self.meeting_id

        # 1. Append to JSONL and flush immediately
        self._jsonl_file.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._jsonl_file.flush()

        # 2. Insert to SQLite
        cur = self._conn.cursor()
        ev_type = ev.get("type", "unknown")
        seq = ev["seq"]
        ts = ev["ts"]
        mid = ev.get("meeting_id")
        pid = ev.get("participant_id")
        pkey = ev.get("person_key")
        name = ev.get("name") or ev.get("speaker")
        source = ev.get("source")
        conf = ev.get("confidence")

        payload = {k: v for k, v in ev.items() if k not in (
            "seq", "ts", "type", "meeting_id", "participant_id", "person_key", "name", "speaker", "source", "confidence"
        )}

        cur.execute(
            """INSERT OR REPLACE INTO events
               (seq, ts, type, meeting_id, participant_id, person_key, name, source, confidence, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (seq, ts, ev_type, mid, pid, pkey, name, source, conf, json.dumps(payload, ensure_ascii=False))
        )

        if ev_type == "snapshot":
            cur.execute(
                """INSERT OR REPLACE INTO snapshots
                   (seq, ts, total, unique_people, connections, cam_on, cam_off, cam_unknown,
                    mic_on, mic_off, mic_unknown, desktop_count, mobile_count, dialin_count, unknown_device_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    seq, ts,
                    ev.get("total", 0),
                    ev.get("unique_people", ev.get("total", 0)),
                    ev.get("connections", ev.get("total", 0)),
                    ev.get("cam_on", 0),
                    ev.get("cam_off", 0),
                    ev.get("cam_unknown", 0),
                    ev.get("mic_on", 0),
                    ev.get("mic_off", 0),
                    ev.get("mic_unknown", 0),
                    ev.get("desktop_count", 0),
                    ev.get("mobile_count", 0),
                    ev.get("dialin_count", 0),
                    ev.get("unknown_device_count", 0),
                )
            )

        if pid and (ev_type in ("participant_join", "participant_state", "heartbeat_participant")):
            cur.execute(
                """INSERT INTO participants
                   (participant_id, person_key, name, device, device_basis, device_confidence, cam, mic, first_seen, last_seen, is_present)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(participant_id) DO UPDATE SET
                     name = COALESCE(excluded.name, name),
                     device = CASE WHEN excluded.device != 'unknown' THEN excluded.device ELSE device END,
                     device_basis = COALESCE(excluded.device_basis, device_basis),
                     device_confidence = COALESCE(excluded.device_confidence, device_confidence),
                     cam = COALESCE(excluded.cam, cam),
                     mic = COALESCE(excluded.mic, mic),
                     last_seen = excluded.last_seen,
                     is_present = 1""",
                (
                    pid,
                    pkey or (name or "unknown").lower(),
                    name or "Participant",
                    ev.get("device", "unknown"),
                    ev.get("device_basis", ""),
                    ev.get("device_confidence", ""),
                    ev.get("cam", "unknown"),
                    ev.get("mic", "unknown"),
                    ts,
                    ts,
                )
            )
        elif pid and ev_type == "participant_leave":
            cur.execute(
                """UPDATE participants SET is_present = 0, last_seen = ? WHERE participant_id = ?""",
                (ts, pid)
            )

        self._conn.commit()
        return ev

    def get_events(self, since_seq: int = 0) -> List[Dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute("SELECT seq, ts, type, meeting_id, participant_id, person_key, name, source, confidence, payload FROM events WHERE seq > ? ORDER BY seq ASC", (since_seq,))
        out = []
        for row in cur.fetchall():
            item = {
                "seq": row[0],
                "ts": row[1],
                "type": row[2],
                "meeting_id": row[3],
                "participant_id": row[4],
                "person_key": row[5],
                "name": row[6],
                "source": row[7],
                "confidence": row[8],
            }
            if row[9]:
                try:
                    payload = json.loads(row[9])
                    item.update(payload)
                except Exception:
                    pass
            out.append(item)
        return out

    def close(self) -> None:
        try:
            self._jsonl_file.close()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass

    @staticmethod
    def rebuild_from_jsonl(jsonl_path: str | Path, db_path: str | Path) -> EventStore:
        jsonl_path = Path(jsonl_path)
        db_path = Path(db_path)
        if db_path.exists():
            db_path.unlink()
        store = EventStore(out_dir=db_path.parent)
        if jsonl_path.exists():
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.trim() if hasattr(line, "trim") else line.strip()
                    if line:
                        try:
                            ev = json.loads(line)
                            store.record_event(ev)
                        except Exception:
                            continue
        return store
