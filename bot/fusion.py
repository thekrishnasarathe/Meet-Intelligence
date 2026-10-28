"""
fusion.py - Signal fusion for camera, mic, and device state (WP4 & WP5).

Merges tile signals and People-panel signals into one coherent state per participant.
Hysteresis prevents UI flicker from emitting false state transitions.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def normalize_person_key(name: str) -> str:
    """Normalize a display name into a stable person_key (WP3)."""
    if not name:
        return "unknown"
    clean = re.sub(r"\s+", " ", name).strip()
    clean = re.sub(r"\s*\((?:you|presentation|guest|host|co-host|meeting host)\)$", "", clean, flags=re.I)
    clean = re.sub(r"\s+(?:meeting host|host|co-host)$", "", clean, flags=re.I)
    return clean.strip().lower()


def is_phone_number(text: str) -> bool:
    """Detect if string matches a telephone number pattern."""
    if not text:
        return False
    digits = re.sub(r"\D", "", text)
    if 7 <= len(digits) <= 15:
        if re.search(r"^[\s+]*\+?\d{1,4}?[-.\s]?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}[\s]*$", text):
            return True
        if re.search(r"^\+?\d{7,15}$", re.sub(r"[-.\s()]", "", text)):
            return True
    return False


@dataclass
class ParticipantState:
    participant_id: str
    name: str
    person_key: str
    cam: str = "unknown"             # on | off | unknown
    cam_source: str = "init"
    cam_confidence: str = "low"      # high | medium | low
    mic: str = "unknown"             # on | off | unknown
    mic_source: str = "init"
    mic_confidence: str = "low"
    device: str = "unknown"          # desktop | mobile | phone_dial_in | unknown
    device_basis: str = "init"
    device_confidence: str = "low"
    is_present: bool = True
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    # Hysteresis tracking
    pending_cam: Optional[str] = None
    pending_cam_since: float = 0.0
    pending_mic: Optional[str] = None
    pending_mic_since: float = 0.0


class SignalFusion:
    def __init__(self, hysteresis_sec: float = 1.5, bot_name: str = "Notes Bot"):
        self.hysteresis_sec = hysteresis_sec
        self.bot_name = bot_name
        self.bot_key = normalize_person_key(bot_name)
        self.participants: Dict[str, ParticipantState] = {}
        self.official_headcount: Optional[int] = None

    def update_tile_signal(
        self,
        participant_id: str,
        name: str,
        video_active: Optional[bool],
        tile_muted: Optional[bool],
        is_self: bool = False,
        video_width: int = 0,
        video_height: int = 0,
        has_phone_icon: bool = False,
        now: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Ingest observable signals from a grid tile."""
        now = now or time.time()
        events: List[Dict[str, Any]] = []

        person_key = normalize_person_key(name)
        if is_self or person_key == self.bot_key or name == "You":
            return events

        p = self.participants.get(participant_id)
        if not p:
            p = ParticipantState(
                participant_id=participant_id,
                name=name,
                person_key=person_key,
                first_seen=now,
                last_seen=now
            )
            self.participants[participant_id] = p
            events.append({
                "type": "participant_join",
                "participant_id": participant_id,
                "person_key": person_key,
                "name": name,
                "source": "tile",
                "confidence": "high",
                "device": "unknown"
            })

        p.last_seen = now
        if name and name != p.name and not name.lower().startswith("participant"):
            p.name = name
            p.person_key = person_key

        # 1. Camera state from tile video
        target_cam = "unknown"
        cam_source = "off_screen"
        cam_conf = "low"

        if video_active is True:
            target_cam = "on"
            cam_source = "tile_video"
            cam_conf = "high"
        elif video_active is False:
            target_cam = "off"
            cam_source = "tile_video_paused"
            cam_conf = "high"

        # Apply camera hysteresis
        cam_ev = self._apply_cam_hysteresis(p, target_cam, cam_source, cam_conf, now)
        if cam_ev:
            events.append(cam_ev)

        # 2. Mic state from tile indicator
        if tile_muted is not None:
            target_mic = "off" if tile_muted else "on"
            mic_source = "tile_indicator"
            mic_conf = "high"
            mic_ev = self._apply_mic_hysteresis(p, target_mic, mic_source, mic_conf, now)
            if mic_ev:
                events.append(mic_ev)

        # 3. Device detection (WP5)
        dev_ev = self._eval_device(p, name, has_phone_icon, video_width, video_height)
        if dev_ev:
            events.append(dev_ev)

        return events

    def update_panel_signal(
        self,
        participant_id: str,
        name: str,
        panel_muted: Optional[bool],
        has_phone_icon: bool = False,
        is_self: bool = False,
        now: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """Ingest signals from the People panel roster row."""
        now = now or time.time()
        events: List[Dict[str, Any]] = []

        person_key = normalize_person_key(name)
        if is_self or person_key == self.bot_key or name == "You":
            return events

        p = self.participants.get(participant_id)
        if not p:
            p = ParticipantState(
                participant_id=participant_id,
                name=name,
                person_key=person_key,
                first_seen=now,
                last_seen=now
            )
            self.participants[participant_id] = p
            events.append({
                "type": "participant_join",
                "participant_id": participant_id,
                "person_key": person_key,
                "name": name,
                "source": "panel",
                "confidence": "high",
                "device": "unknown"
            })

        p.last_seen = now
        if name and name != p.name:
            p.name = name
            p.person_key = person_key

        if panel_muted is not None:
            target_mic = "off" if panel_muted else "on"
            mic_source = "panel_row"
            mic_conf = "high"
            mic_ev = self._apply_mic_hysteresis(p, target_mic, mic_source, mic_conf, now)
            if mic_ev:
                events.append(mic_ev)

        dev_ev = self._eval_device(p, name, has_phone_icon, 0, 0)
        if dev_ev:
            events.append(dev_ev)

        return events

    def _apply_cam_hysteresis(
        self, p: ParticipantState, target: str, source: str, conf: str, now: float
    ) -> Optional[Dict[str, Any]]:
        if target == p.cam:
            p.pending_cam = None
            return None

        if p.pending_cam != target:
            p.pending_cam = target
            p.pending_cam_since = now
            return None

        if (now - p.pending_cam_since) >= self.hysteresis_sec:
            p.cam = target
            p.cam_source = source
            p.cam_confidence = conf
            p.pending_cam = None
            return {
                "type": "cam_change",
                "participant_id": p.participant_id,
                "person_key": p.person_key,
                "name": p.name,
                "state": target,
                "source": source,
                "confidence": conf
            }
        return None

    def _apply_mic_hysteresis(
        self, p: ParticipantState, target: str, source: str, conf: str, now: float
    ) -> Optional[Dict[str, Any]]:
        if target == p.mic:
            p.pending_mic = None
            return None

        if p.pending_mic != target:
            p.pending_mic = target
            p.pending_mic_since = now
            return None

        if (now - p.pending_mic_since) >= self.hysteresis_sec:
            p.mic = target
            p.mic_source = source
            p.mic_confidence = conf
            p.pending_mic = None
            return {
                "type": "mic_change",
                "participant_id": p.participant_id,
                "person_key": p.person_key,
                "name": p.name,
                "state": target,
                "source": source,
                "confidence": conf
            }
        return None
        

    def _eval_device(
        self, p: ParticipantState, name: str, has_phone_icon: bool, vw: int, vh: int
    ) -> Optional[Dict[str, Any]]:
        old_dev = p.device
        new_dev = old_dev
        basis = p.device_basis
        conf = p.device_confidence

        if has_phone_icon or is_phone_number(name):
            new_dev = "phone_dial_in"
            basis = "phone_number_or_icon"
            conf = "high"
        elif vw > 0 and vh > 0:
            if vh > vw:
                new_dev = "likely_mobile"
                basis = "portrait_video"
                conf = "low"
            elif vw >= vh * 1.15:
                if old_dev not in ("phone_dial_in", "likely_mobile"):
                    new_dev = "likely_desktop"
                    basis = "landscape_video"
                    conf = "low"

        if new_dev != old_dev:
            p.device = new_dev
            p.device_basis = basis
            p.device_confidence = conf
            return {
                "type": "device_detected",
                "participant_id": p.participant_id,
                "person_key": p.person_key,
                "name": p.name,
                "device": new_dev,
                "basis": basis,
                "confidence": conf
            }
        return None

    def build_snapshot(self) -> Dict[str, Any]:
        """Aggregate counts across all active participants (WP4)."""
        active = [p for p in self.participants.values() if p.is_present]
        unique_keys = set(p.person_key for p in active)

        cam_on = sum(1 for p in active if p.cam == "on")
        cam_off = sum(1 for p in active if p.cam == "off")
        cam_unknown = sum(1 for p in active if p.cam == "unknown")

        mic_on = sum(1 for p in active if p.mic == "on")
        mic_off = sum(1 for p in active if p.mic == "off")
        mic_unknown = sum(1 for p in active if p.mic == "unknown")

        desktop_count = sum(1 for p in active if p.device in ("desktop", "likely_desktop"))
        mobile_count = sum(1 for p in active if p.device in ("mobile", "likely_mobile"))
        dialin_count = sum(1 for p in active if p.device == "phone_dial_in")
        unknown_device = sum(1 for p in active if p.device == "unknown")

        total = max(len(active), self.official_headcount or len(active))

        return {
            "type": "snapshot",
            "total": total,
            "unique_people": len(unique_keys),
            "connections": len(active),
            "cam_on": cam_on,
            "cam_off": cam_off,
            "cam_unknown": cam_unknown,
            "mic_on": mic_on,
            "mic_off": mic_off,
            "mic_unknown": mic_unknown,
            "desktop_count": desktop_count,
            "mobile_count": mobile_count,
            "dialin_count": dialin_count,
            "unknown_device_count": unknown_device
        }

    def build_heartbeat(self) -> Dict[str, Any]:
        """Full state heartbeat event every 10 seconds."""
        active = [p for p in self.participants.values() if p.is_present]
        part_states = []
        for p in active:
            part_states.append({
                "participant_id": p.participant_id,
                "person_key": p.person_key,
                "name": p.name,
                "cam": p.cam,
                "mic": p.mic,
                "device": p.device,
                "device_basis": p.device_basis,
                "device_confidence": p.device_confidence
            })
        snap = self.build_snapshot()
        return {
            "type": "heartbeat",
            "snapshot": snap,
            "participants": part_states
        }
