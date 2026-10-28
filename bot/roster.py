"""
roster.py - People-panel roster tracker and join/leave debounce diff (WP3).

Reads the People panel list (ground truth for headcount), scrolls virtualized lists,
tracks connections and unique people, and emits debounced join/leave events.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


def normalize_person_key(name: str) -> str:
    if not name:
        return "unknown"
    clean = re.sub(r"\s+", " ", name).strip()
    clean = re.sub(r"\s*\((?:you|presentation|guest|host|co-host|meeting host)\)$", "", clean, flags=re.I)
    clean = re.sub(r"\s+(?:meeting host|host|co-host)$", "", clean, flags=re.I)
    return clean.strip().lower()


@dataclass
class RosterEntry:
    participant_id: str
    name: str
    person_key: str
    is_muted: Optional[bool] = None
    has_phone_icon: bool = False
    is_self: bool = False
    missing_count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class RosterTracker:
    def __init__(self, debounce_missing: int = 3, bot_name: str = "Notes Bot"):
        self.debounce_missing = debounce_missing
        self.bot_name = bot_name
        self.bot_key = normalize_person_key(bot_name)
        self.active_roster: Dict[str, RosterEntry] = {}
        self.official_count: Optional[int] = None

    def reconcile_scan(
        self,
        scanned_items: List[Dict[str, Any]],
        official_badge_count: Optional[int] = None,
        tile_ids: Optional[Set[str]] = None,
        now: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Reconcile a roster scan from the People panel.
        scanned_items: list of dicts with keys:
          - participant_id: str
          - name: str
          - is_muted: Optional[bool]
          - has_phone_icon: bool
          - is_self: bool
        """
        now = now or time.time()
        events: List[Dict[str, Any]] = []
        self.official_count = official_badge_count

        seen_ids: Set[str] = set()

        for item in scanned_items:
            raw_name = item.get("name", "Participant").strip()
            is_self = bool(item.get("is_self")) or raw_name == "You" or "(You)" in raw_name
            person_key = normalize_person_key(raw_name)

            # Exclude bot itself from the participant roster
            if is_self or person_key == self.bot_key or raw_name == self.bot_name:
                continue

            pid = item.get("participant_id") or ("roster_" + person_key)
            seen_ids.add(pid)

            entry = self.active_roster.get(pid)
            if not entry:
                entry = RosterEntry(
                    participant_id=pid,
                    name=raw_name,
                    person_key=person_key,
                    is_muted=item.get("is_muted"),
                    has_phone_icon=bool(item.get("has_phone_icon")),
                    is_self=is_self,
                    first_seen=now,
                    last_seen=now
                )
                self.active_roster[pid] = entry
                events.append({
                    "type": "roster_join",
                    "participant_id": pid,
                    "person_key": person_key,
                    "name": raw_name,
                    "is_muted": item.get("is_muted"),
                    "has_phone_icon": bool(item.get("has_phone_icon")),
                    "source": "people_panel"
                })
            else:
                entry.missing_count = 0
                entry.last_seen = now
                if raw_name and raw_name != entry.name:
                    entry.name = raw_name
                    entry.person_key = person_key
                entry.is_muted = item.get("is_muted")
                entry.has_phone_icon = bool(item.get("has_phone_icon"))

        # Debounced leave handling
        to_delete = []
        for pid, entry in self.active_roster.items():
            if pid not in seen_ids:
                entry.missing_count += 1
                if entry.missing_count >= self.debounce_missing:
                    events.append({
                        "type": "roster_leave",
                        "participant_id": pid,
                        "person_key": entry.person_key,
                        "name": entry.name,
                        "source": "people_panel",
                        "duration_sec": round(now - entry.first_seen, 2)
                    })
                    to_delete.append(pid)

        for pid in to_delete:
            del self.active_roster[pid]

        # Check for roster mismatch against official count badge
        roster_count = len(self.active_roster)
        if official_badge_count is not None:
            # Note: official badge count usually includes self/bot, so compare (roster + 1) or check tolerance
            expected_participant_count = max(0, official_badge_count - 1)
            if abs(roster_count - expected_participant_count) > 1:
                events.append({
                    "type": "roster_mismatch",
                    "roster_count": roster_count,
                    "official_badge_count": official_badge_count,
                    "delta": roster_count - expected_participant_count,
                    "source": "people_panel_vs_badge"
                })

        # Check for tile mismatch if tile IDs were supplied
        if tile_ids is not None:
            active_pids = set(self.active_roster.keys())
            missing_tiles = active_pids - tile_ids
            if missing_tiles:
                events.append({
                    "type": "tiles_unrendered",
                    "unrendered_count": len(missing_tiles),
                    "unrendered_ids": list(missing_tiles),
                    "observable_tiles": len(tile_ids),
                    "total_roster": roster_count
                })

        return events

    def get_summary(self) -> Dict[str, Any]:
        unique_keys = set(e.person_key for e in self.active_roster.values())
        return {
            "connections": len(self.active_roster),
            "unique_people": len(unique_keys),
            "participants": [
                {
                    "participant_id": e.participant_id,
                    "person_key": e.person_key,
                    "name": e.name,
                    "is_muted": e.is_muted,
                    "has_phone_icon": e.has_phone_icon
                }
                for e in self.active_roster.values()
            ]
        }
