"""
test_bot_core.py - Comprehensive unit tests for bot core components:
fusion.py, roster.py, store.py, analytics.py.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))

from analytics import MeetingAnalytics
from fusion import SignalFusion, normalize_person_key, is_phone_number
from roster import RosterTracker
from store import EventStore


class TestFusion(unittest.TestCase):
    def setUp(self):
        self.fusion = SignalFusion(hysteresis_sec=1.5, bot_name="Notes Bot")

    def test_normalize_person_key(self):
        self.assertEqual(normalize_person_key("Krishna Sarathe (You)"), "krishna sarathe")
        self.assertEqual(normalize_person_key("Alice Smith Meeting host"), "alice smith")
        self.assertEqual(normalize_person_key("Bob (Guest)"), "bob")

    def test_phone_number_detection(self):
        self.assertTrue(is_phone_number("+1 (555) 234-5678"))
        self.assertTrue(is_phone_number("+91 98765 43210"))
        self.assertFalse(is_phone_number("Alice Smith"))

    def test_bot_exclusion(self):
        evs = self.fusion.update_tile_signal("bot_tile", "Notes Bot", True, False, is_self=True)
        self.assertEqual(len(evs), 0)
        self.assertNotIn("bot_tile", self.fusion.participants)

    def test_camera_and_mic_hysteresis(self):
        t0 = 1000.0
        # First emission: should not emit state change until hysteresis passes
        evs1 = self.fusion.update_tile_signal("p1", "Alice", True, True, now=t0)
        # join event emitted, but cam_change and mic_change pending
        join_evs = [e for e in evs1 if e["type"] == "participant_join"]
        self.assertEqual(len(join_evs), 1)
        self.assertEqual(len([e for e in evs1 if e["type"] == "cam_change"]), 0)

        # After 0.8s (under 1.5s): still pending
        evs2 = self.fusion.update_tile_signal("p1", "Alice", True, True, now=t0 + 0.8)
        self.assertEqual(len([e for e in evs2 if e["type"] == "cam_change"]), 0)

        # After 1.6s (> 1.5s): state change emitted!
        evs3 = self.fusion.update_tile_signal("p1", "Alice", True, True, now=t0 + 1.6)
        cam_evs = [e for e in evs3 if e["type"] == "cam_change"]
        mic_evs = [e for e in evs3 if e["type"] == "mic_change"]
        self.assertEqual(len(cam_evs), 1)
        self.assertEqual(cam_evs[0]["state"], "on")
        self.assertEqual(len(mic_evs), 1)
        self.assertEqual(mic_evs[0]["state"], "off")

    def test_device_classification(self):
        t0 = 1000.0
        # Dial-in participant
        evs = self.fusion.update_tile_signal("p_dial", "+1 555-0199", False, True, has_phone_icon=True, now=t0)
        p = self.fusion.participants["p_dial"]
        self.assertEqual(p.device, "phone_dial_in")

        # Portrait video mobile participant
        evs2 = self.fusion.update_tile_signal("p_mob", "Mobile User", True, True, video_width=720, video_height=1280, now=t0)
        p_mob = self.fusion.participants["p_mob"]
        self.assertEqual(p_mob.device, "likely_mobile")

        # Landscape desktop participant
        evs3 = self.fusion.update_tile_signal("p_desk", "Desktop User", True, True, video_width=1280, video_height=720, now=t0)
        p_desk = self.fusion.participants["p_desk"]
        self.assertEqual(p_desk.device, "likely_desktop")


class TestRoster(unittest.TestCase):
    def setUp(self):
        self.roster = RosterTracker(debounce_missing=3, bot_name="Notes Bot")

    def test_roster_join_and_debounce_leave(self):
        t0 = 1000.0
        # Scan 1: Alice and Bob present
        items1 = [
            {"participant_id": "r1", "name": "Alice", "is_muted": False},
            {"participant_id": "r2", "name": "Bob", "is_muted": True},
            {"participant_id": "r_bot", "name": "Notes Bot", "is_self": True}
        ]
        evs1 = self.roster.reconcile_scan(items1, official_badge_count=3, now=t0)
        self.assertEqual(len(self.roster.active_roster), 2)
        join_names = [e["name"] for e in evs1 if e["type"] == "roster_join"]
        self.assertIn("Alice", join_names)
        self.assertIn("Bob", join_names)
        self.assertNotIn("Notes Bot", join_names)

        # Scan 2: Bob missing 1st time -> not left yet
        items2 = [{"participant_id": "r1", "name": "Alice", "is_muted": False}]
        evs2 = self.roster.reconcile_scan(items2, now=t0 + 2)
        self.assertEqual(len([e for e in evs2 if e["type"] == "roster_leave"]), 0)
        self.assertEqual(len(self.roster.active_roster), 2)

        # Scan 3: Bob missing 2nd time -> still not left
        evs3 = self.roster.reconcile_scan(items2, now=t0 + 4)
        self.assertEqual(len([e for e in evs3 if e["type"] == "roster_leave"]), 0)

        # Scan 4: Bob missing 3rd time -> debounced leave emitted!
        evs4 = self.roster.reconcile_scan(items2, now=t0 + 6)
        leave_evs = [e for e in evs4 if e["type"] == "roster_leave"]
        self.assertEqual(len(leave_evs), 1)
        self.assertEqual(leave_evs[0]["participant_id"], "r2")
        self.assertEqual(len(self.roster.active_roster), 1)


class TestStoreAndAnalytics(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store = EventStore(out_dir=self.test_dir, meeting_id="test-meet")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_store_and_analytics_pipeline(self):
        events = [
            {"type": "call_started", "ts": "2026-10-28T07:00:00Z", "meeting_id": "test-meet"},
            {"type": "participant_join", "ts": "2026-10-28T07:00:05Z", "participant_id": "p1", "name": "Asha", "device": "desktop"},
            {"type": "cam_change", "ts": "2026-10-28T07:00:10Z", "participant_id": "p1", "state": "on"},
            {"type": "mic_change", "ts": "2026-10-28T07:00:15Z", "participant_id": "p1", "state": "on"},
            {"type": "mic_change", "ts": "2026-10-28T07:00:45Z", "participant_id": "p1", "state": "off"},
            {"type": "participant_join", "ts": "2026-10-28T07:00:20Z", "participant_id": "p2", "name": "Rahul", "device": "likely_mobile"},
            {"type": "snapshot", "ts": "2026-10-28T07:00:30Z", "total": 2, "unique_people": 2, "cam_on": 1, "mic_on": 1},
            {"type": "participant_leave", "ts": "2026-10-28T07:01:00Z", "participant_id": "p2", "name": "Rahul"},
            {"type": "call_ended", "ts": "2026-10-28T07:02:00Z", "meeting_id": "test-meet"}
        ]
        for ev in events:
            self.store.record_event(ev)

        # Verify jsonl content
        jsonl_path = Path(self.test_dir) / "events.jsonl"
        self.assertTrue(jsonl_path.exists())
        lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), len(events))

        # Verify SQLite DB
        stored = self.store.get_events()
        self.assertEqual(len(stored), len(events))
        self.assertEqual(stored[0]["seq"], 1)

        # Run analytics on the stored events
        analytics = MeetingAnalytics.from_jsonl(jsonl_path)
        stats = analytics.compute_stats()
        self.assertEqual(stats["meeting_id"], "test-meet")
        self.assertEqual(stats["total_unique_participants"], 2)
        self.assertEqual(stats["peak_concurrent"], 2)

        # Test Markdown table rendering
        md_table = analytics.render_markdown_table()
        self.assertIn("## Participation and Engagement", md_table)
        self.assertIn("Asha", md_table)
        self.assertIn("Rahul", md_table)
        self.assertIn("Mobile", md_table)


if __name__ == "__main__":
    unittest.main()
