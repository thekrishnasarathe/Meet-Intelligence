"""
analytics.py - Meeting metrics, engagement math, and participation tables (WP7).
Reuses reference calculations from extension/lib.js (durations, concurrency curves, timelines).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def parse_iso(ts_str: Optional[str]) -> float:
    if not ts_str:
        return 0.0
    try:
        # Replace trailing Z with UTC timezone
        clean = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.timestamp() * 1000.0
    except Exception:
        return 0.0


def fmt_dur(ms: float) -> str:
    s = max(0, round(ms / 1000.0))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60:02d}s"
    h = m // 60
    return f"{h}h {m % 60:02d}m"


def fmt_clock(ms: float) -> str:
    total = max(0, int(ms / 1000.0))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return (f"{h:02d}:" if h else "") + f"{m:02d}:{s:02d}"


class MeetingAnalytics:
    def __init__(self, events: List[Dict[str, Any]]):
        self.events = sorted(events, key=lambda e: parse_iso(e.get("ts")))
        self.meeting_id = "unknown"
        self.started_at: Optional[str] = None
        self.ended_at: Optional[str] = None

        for e in self.events:
            if not self.meeting_id or self.meeting_id == "unknown":
                if e.get("meeting_id"):
                    self.meeting_id = e["meeting_id"]
            if e.get("type") == "call_started" and not self.started_at:
                self.started_at = e.get("ts")
            elif e.get("type") == "call_ended":
                self.ended_at = e.get("ts")

        if not self.started_at and self.events:
            self.started_at = self.events[0].get("ts")
        if not self.ended_at and self.events:
            self.ended_at = self.events[-1].get("ts")

    @classmethod
    def from_jsonl(cls, jsonl_path: str | Path) -> MeetingAnalytics:
        events = []
        path = Path(jsonl_path)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except Exception:
                            continue
        return cls(events)

    def compute_stats(self) -> Dict[str, Any]:
        start_ms = parse_iso(self.started_at)
        end_ms = parse_iso(self.ended_at)
        total_duration_ms = max(0.0, end_ms - start_ms)

        # Track presence, camera on, mic unmuted, and unknown states
        participants: Dict[str, Dict[str, Any]] = {}
        snapshots: List[Dict[str, Any]] = []
        timeline: List[Dict[str, Any]] = []

        def accrue(p: Dict[str, Any], t: float):
            if p["present"] and p["last_ts"] is not None and t > p["last_ts"]:
                delta = t - p["last_ts"]
                p["present_ms"] += delta
                if p["cam"] == "on":
                    p["cam_on_ms"] += delta
                elif p["cam"] == "unknown":
                    p["cam_unknown_ms"] += delta

                if p["mic"] == "on":
                    p["mic_on_ms"] += delta
                elif p["mic"] == "unknown":
                    p["mic_unknown_ms"] += delta
            p["last_ts"] = t

        for ev in self.events:
            t = parse_iso(ev.get("ts"))
            ev_type = ev.get("type")
            pid = ev.get("participant_id")
            name = ev.get("name") or ev.get("speaker") or "Participant"

            if pid:
                if pid not in participants:
                    participants[pid] = {
                        "participant_id": pid,
                        "person_key": ev.get("person_key") or name.lower(),
                        "name": name,
                        "device": ev.get("device", "unknown"),
                        "device_basis": ev.get("basis", ""),
                        "present": False,
                        "cam": "unknown",
                        "mic": "unknown",
                        "present_ms": 0.0,
                        "cam_on_ms": 0.0,
                        "cam_unknown_ms": 0.0,
                        "mic_on_ms": 0.0,
                        "mic_unknown_ms": 0.0,
                        "last_ts": None,
                        "first_seen": ev.get("ts"),
                        "last_seen": ev.get("ts")
                    }

                p = participants[pid]
                accrue(p, t)
                p["last_seen"] = ev.get("ts")
                if ev.get("name"):
                    p["name"] = ev["name"]
                if ev.get("device") and ev["device"] != "unknown":
                    p["device"] = ev["device"]
                    p["device_basis"] = ev.get("basis", "")

                if ev_type in ("participant_join", "roster_join"):
                    p["present"] = True
                    timeline.append({
                        "offset_ms": max(0.0, t - start_ms),
                        "time_clock": fmt_clock(max(0.0, t - start_ms)),
                        "action": "joined",
                        "name": p["name"],
                        "participant_id": pid
                    })
                elif ev_type in ("participant_leave", "roster_leave"):
                    p["present"] = False
                    timeline.append({
                        "offset_ms": max(0.0, t - start_ms),
                        "time_clock": fmt_clock(max(0.0, t - start_ms)),
                        "action": "left",
                        "name": p["name"],
                        "participant_id": pid
                    })
                elif ev_type in ("cam_on", "cam_change") and ev.get("state") == "on":
                    p["cam"] = "on"
                elif ev_type in ("cam_off", "cam_change") and ev.get("state") == "off":
                    p["cam"] = "off"
                elif ev_type == "cam_change" and ev.get("state") == "unknown":
                    p["cam"] = "unknown"
                elif ev_type in ("mic_on", "mic_change") and ev.get("state") == "on":
                    p["mic"] = "on"
                elif ev_type in ("mic_off", "mic_change") and ev.get("state") == "off":
                    p["mic"] = "off"
                elif ev_type == "mic_change" and ev.get("state") == "unknown":
                    p["mic"] = "unknown"

            elif ev_type == "snapshot":
                snapshots.append({
                    "offset_ms": max(0.0, t - start_ms),
                    "total": ev.get("total", 0),
                    "unique_people": ev.get("unique_people", 0),
                    "cam_on": ev.get("cam_on", 0),
                    "cam_unknown": ev.get("cam_unknown", 0),
                    "mic_on": ev.get("mic_on", 0),
                    "mic_unknown": ev.get("mic_unknown", 0),
                })

        for p in participants.values():
            accrue(p, end_ms)

        # Merge participants with identical person_key into unified person rows
        merged_people: Dict[str, Dict[str, Any]] = {}
        for p in participants.values():
            pkey = p["person_key"]
            if pkey not in merged_people:
                merged_people[pkey] = {
                    "name": p["name"],
                    "person_key": pkey,
                    "device": p["device"],
                    "present_ms": p["present_ms"],
                    "cam_on_ms": p["cam_on_ms"],
                    "cam_unknown_ms": p["cam_unknown_ms"],
                    "mic_on_ms": p["mic_on_ms"],
                    "mic_unknown_ms": p["mic_unknown_ms"],
                    "connections": [p["participant_id"]]
                }
            else:
                m = merged_people[pkey]
                m["present_ms"] = max(m["present_ms"], p["present_ms"])
                m["cam_on_ms"] += p["cam_on_ms"]
                m["cam_unknown_ms"] += p["cam_unknown_ms"]
                m["mic_on_ms"] += p["mic_on_ms"]
                m["mic_unknown_ms"] += p["mic_unknown_ms"]
                m["connections"].append(p["participant_id"])
                if p["device"] != "unknown" and m["device"] == "unknown":
                    m["device"] = p["device"]

        # Calculate percentages
        rows = []
        for p in merged_people.values():
            pres = p["present_ms"]
            rows.append({
                "name": p["name"],
                "person_key": p["person_key"],
                "device": p["device"],
                "connections_count": len(p["connections"]),
                "present_ms": round(pres),
                "present_fmt": fmt_dur(pres),
                "cam_on_ms": round(p["cam_on_ms"]),
                "cam_fmt": fmt_dur(p["cam_on_ms"]),
                "cam_pct": round(100.0 * p["cam_on_ms"] / pres) if pres > 0 else 0,
                "cam_unknown_ms": round(p["cam_unknown_ms"]),
                "mic_on_ms": round(p["mic_on_ms"]),
                "mic_fmt": fmt_dur(p["mic_on_ms"]),
                "mic_pct": round(100.0 * p["mic_on_ms"] / pres) if pres > 0 else 0,
                "mic_unknown_ms": round(p["mic_unknown_ms"]),
            })

        rows.sort(key=lambda r: r["present_ms"], reverse=True)

        # Concurrency curves: peak and average
        concurrent_counts = [s.get("total", 0) for s in snapshots] if snapshots else [len(rows)]
        peak_concurrent = max(concurrent_counts) if concurrent_counts else len(rows)
        avg_concurrent = round(sum(concurrent_counts) / len(concurrent_counts), 1) if concurrent_counts else len(rows)

        return {
            "meeting_id": self.meeting_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": total_duration_ms,
            "duration_fmt": fmt_dur(total_duration_ms),
            "peak_concurrent": peak_concurrent,
            "average_concurrent": avg_concurrent,
            "total_unique_participants": len(rows),
            "total_connections": sum(r["connections_count"] for r in rows),
            "participants": rows,
            "timeline": timeline,
            "snapshots_count": len(snapshots)
        }

    def render_markdown_table(self) -> str:
        """Render participation table matching WP7 & extension/lib.js specification."""
        stats = self.compute_stats()
        rows = stats["participants"]
        lines = [
            "## Participation and Engagement",
            "",
            f"- **Meeting Duration:** {stats['duration_fmt']}",
            f"- **Peak Concurrent:** {stats['peak_concurrent']} participants",
            f"- **Average Concurrent:** {stats['average_concurrent']} participants",
            f"- **Unique Attendees:** {stats['total_unique_participants']} (connections: {stats['total_connections']})",
            "",
            "| Participant | Device | Present | Camera on | Mic unmuted |",
            "|---|---|---|---|---|",
        ]
        if not rows:
            lines.append("| *(No participants detected)* | - | - | - | - |")
        else:
            for r in rows:
                dev = "Mobile" if "mobile" in r["device"] else ("Phone (dial-in)" if r["device"] == "phone_dial_in" else ("Desktop" if "desktop" in r["device"] else "Unknown"))
                cam_str = f"{r['cam_fmt']} ({r['cam_pct']}%)"
                mic_str = f"{r['mic_fmt']} ({r['mic_pct']}%)"
                lines.append(f"| {r['name']} | {dev} | {r['present_fmt']} | {cam_str} | {mic_str} |")

        lines.extend([
            "",
            "> [!NOTE]",
            "> **Mic unmuted** reflects when the microphone was active in Google Meet, not speech transcription time.",
            "> Devices not directly observable are classified honestly as *Unknown* without guessing."
        ])
        return "\n".join(lines)
