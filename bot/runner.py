"""
runner.py - Main entry point and lifecycle orchestrator for Google Meet Bot (WP1-WP9).

Integrates:
- ControlledBrowser (WP1 & WP2)
- RosterTracker (WP3)
- SignalFusion (WP4 & WP5)
- EventStore (WP6)
- MeetingAnalytics (WP7)
- ReportBuilder (WP8 - report.py)
- Reliability, consent message, heartbeat status.json (WP9)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from analytics import MeetingAnalytics
from browser import ControlledBrowser
from fusion import SignalFusion
from report import ReportBuilder, gemini_llm, groq_llm, turns_from_jsonl
from roster import RosterTracker
from store import EventStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MeetBot.Runner")


def extract_meeting_id(url: str) -> str:
    m = re.search(r"([a-z]{3}-[a-z]{4}-[a-z]{3})", url, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    parts = [p for p in url.rstrip("/").split("/") if p]
    last = parts[-1] if parts else "unknown"
    cleaned = last.split("?")[0]
    return cleaned if cleaned else "unknown"


def load_groq_api_key() -> str:
    """Read the Groq API key from GROQ_API_KEY env or backend/.env."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    for candidate in [
        Path(__file__).parent.parent / "backend" / ".env",
        Path(__file__).parent / ".env",
        Path.cwd() / "backend" / ".env",
        Path.cwd() / ".env",
    ]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GROQ_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        os.environ["GROQ_API_KEY"] = val
                        return val
    return ""


class BotRunner:
    def __init__(
        self,
        url: str,
        name: str = "Notes Bot",
        out_dir: str = "./session_output",
        profile_dir: str = "./bot-profile",
        max_duration_min: int = 180,
        max_lobby_wait_sec: int = 300,
        headless: bool = False,
        use_chrome: bool = False,
        backend_url: str = "",
        consent_message: str = "Notes Bot is transcribing this meeting.",
        enable_chat: bool = True,
    ):
        self.url = url
        self.name = name
        self.meeting_id = extract_meeting_id(url)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = profile_dir
        self.max_duration_sec = max_duration_min * 60
        self.max_lobby_wait_sec = max_lobby_wait_sec
        self.headless = headless
        self.use_chrome = use_chrome
        self.backend_url = backend_url.rstrip("/") if backend_url else ""
        self.consent_message = consent_message
        self.enable_chat = enable_chat

        # Internal components
        self.store = EventStore(out_dir=self.out_dir, meeting_id=self.meeting_id)
        self.roster = RosterTracker(bot_name=self.name)
        self.fusion = SignalFusion(bot_name=self.name)
        self.browser: Optional[ControlledBrowser] = None

        # Report builder setup - reads Groq key from GROQ_API_KEY
        chk_path = self.out_dir / "report_checkpoint.json"
        groq_key = load_groq_api_key()
        gemini_key = os.environ.get("GEMINI_API_KEY", "")

        if groq_key:
            llm = groq_llm(groq_key, model="llama-3.3-70b-versatile")
            self.report_builder = ReportBuilder(llm, checkpoint=str(chk_path), tpm=6000)
        elif gemini_key:
            llm = gemini_llm(gemini_key, model="gemini-1.5-flash")
            self.report_builder = ReportBuilder(llm, checkpoint=str(chk_path))
        else:
            self.report_builder = None

        self.is_running = False
        self.stop_requested = False
        self.start_time = 0.0
        self.last_feed_time = 0.0
        self.has_recorded_first_caption = False
        self.caption_segments: Dict[str, Dict[str, Any]] = {}

    def _update_status_file(self) -> None:
        """Write heartbeat status.json so external processes can monitor liveness (WP9)."""
        snap = self.fusion.build_snapshot()
        status_data = {
            "meeting_id": self.meeting_id,
            "bot_name": self.name,
            "status": "running" if self.is_running else "stopped",
            "uptime_sec": round(time.time() - self.start_time, 1) if self.start_time else 0,
            "participants_total": snap["total"],
            "unique_people": snap["unique_people"],
            "camera_on": snap["cam_on"],
            "mic_on": snap["mic_on"],
            "captions_count": len(self.caption_segments),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            status_file = self.out_dir / "status.json"
            status_file.write_text(json.dumps(status_data, indent=2), encoding="utf-8")
        except Exception:
            pass

    async def start(self) -> None:
        """Main execution loop with reconnect resilience."""
        self.is_running = True
        self.start_time = time.time()
        self.last_feed_time = time.time()

        # Record call_started
        self.store.record_event({
            "type": "call_started",
            "meeting_id": self.meeting_id,
            "source": "runner",
            "confidence": "high"
        })

        reconnect_attempts = 0
        max_reconnects = 3

        while not self.stop_requested and reconnect_attempts < max_reconnects:
            success = False
            try:
                success = await self._run_session()
                if success or self.stop_requested:
                    break
            except Exception as e:
                logger.error(f"Error during bot session: {e}", exc_info=True)
                if self.browser:
                    await self.browser.capture_debug_screenshot(str(e), self.out_dir)
            finally:
                if not success and not self.stop_requested:
                    if self.browser:
                        await self.browser.close()
                        self.browser = None

            if not self.stop_requested:
                reconnect_attempts += 1
                wait_sec = reconnect_attempts * 5
                logger.warning(f"Reconnecting attempt {reconnect_attempts}/{max_reconnects} in {wait_sec}s...")
                await asyncio.sleep(wait_sec)

        await self._finalize_run()

    async def _run_session(self) -> bool:
        print("[STATUS] Launching browser...", flush=True)
        self.browser = ControlledBrowser(
            profile_dir=self.profile_dir,
            headless=self.headless,
            use_chrome=self.use_chrome,
            out_dir=self.out_dir,
        )
        await self.browser.launch()

        joined = await self.browser.join_meeting(
            url=self.url,
            name=self.name,
            max_lobby_wait_sec=self.max_lobby_wait_sec,
            out_dir=self.out_dir,
        )
        if not joined:
            return False

        # Expose Python callback to page for scraper events
        await self.browser.page.expose_function("__meet_intel_emit", self._handle_page_event)

        # Inject scraper scripts via evaluate (bypasses Meet CSP TrustedScript restrictions)
        core_js_path = Path(__file__).parent.parent / "extension" / "meet-core.js"
        if core_js_path.exists():
            await self.browser.page.evaluate(core_js_path.read_text(encoding="utf-8"))

        scraper_js_path = Path(__file__).parent / "scraper.js"
        if scraper_js_path.exists():
            await self.browser.page.evaluate(scraper_js_path.read_text(encoding="utf-8"))

        # Start scraper v2 inside Meet page
        await self.browser.page.evaluate("""() => {
            if (window.MeetScraperV2 && window.MeetScraperV2.start) {
                window.MeetScraperV2.start((ev) => {
                    if (window.__meet_intel_emit) window.__meet_intel_emit(ev);
                });
            } else if (window.MeetCore && window.MeetCore.start) {
                window.MeetCore.start((ev) => {
                    if (window.__meet_intel_emit) window.__meet_intel_emit(ev);
                });
            }
        }""")

        print("[STATUS] Recording: live captions and telemetry active.", flush=True)

        # Post consent notice in chat (WP9)
        if self.enable_chat and self.consent_message:
            await asyncio.sleep(3)
            await self.browser.send_chat_message(self.consent_message)

        # Main supervision loop
        last_heartbeat = time.time()
        while not self.stop_requested:
            now = time.time()
            if (now - self.start_time) > self.max_duration_sec:
                logger.info("Max duration reached. Exiting gracefully.")
                break

            # Heartbeat every 10 seconds (WP4 & WP9)
            if now - last_heartbeat >= 10.0:
                hb = self.fusion.build_heartbeat()
                self.store.record_event(hb)
                self._update_status_file()
                last_heartbeat = now

            # Feed live turns to ReportBuilder roughly every minute (WP8)
            if now - self.last_feed_time >= 60.0:
                await self._feed_report_builder()
                self.last_feed_time = now

            # Check if Leave call button is still present (call ended check)
            try:
                leave_btn = await self.browser.page.query_selector(
                    "button[aria-label*='Leave call' i], button[aria-label*='Leave' i]"
                )
                if not leave_btn or not await leave_btn.is_visible():
                    print("[STATUS] Meeting call ended.", flush=True)
                    logger.info("Call appears ended (leave button gone).")
                    break
            except Exception:
                break

            await asyncio.sleep(2)

        return True

    def _handle_page_event(self, ev: Dict[str, Any]) -> None:
        """Process event emitted from in-page scraper."""
        ev_type = ev.get("type")

        # 1. Record raw event
        self.store.record_event(ev)

        # 2. Forward to backend if configured
        if self.backend_url:
            self._forward_to_backend(ev)

        # 3. Handle caption segment
        if ev_type == "caption":
            cid = ev.get("id")
            if cid:
                self.caption_segments[cid] = ev
            if not self.has_recorded_first_caption and ev.get("text"):
                self.has_recorded_first_caption = True
                spk = ev.get("speaker", "Speaker")
                txt = ev.get("text", "")
                print(f"[STATUS] Caption recorded! [{spk}]: {txt}", flush=True)
                logger.info(f"First caption recorded! [{spk}]: {txt}")

        # 4. Handle participant tile signals
        elif ev_type in ("participant_join", "participant_update", "cam_on", "cam_off", "mic_on", "mic_off"):
            pid = ev.get("id")
            name = ev.get("name", "Participant")
            vid_active = (ev_type == "cam_on") if ev_type in ("cam_on", "cam_off") else None
            muted = (ev_type == "mic_off") if ev_type in ("mic_on", "mic_off") else None

            fused_events = self.fusion.update_tile_signal(
                participant_id=pid,
                name=name,
                video_active=vid_active,
                tile_muted=muted,
                is_self=bool(ev.get("isSelf"))
            )
            for fev in fused_events:
                self.store.record_event(fev)

        # 5. Handle snapshot
        elif ev_type == "snapshot":
            self.fusion.official_headcount = ev.get("total")

    async def _feed_report_builder(self) -> None:
        """Feed updated turns to token-budgeted ReportBuilder (WP8)."""
        if not self.report_builder:
            return
        if self.store and self.store.jsonl_path.exists():
            try:
                turns = turns_from_jsonl(self.store.jsonl_path, meeting_id=self.meeting_id)
                if turns:
                    self.report_builder.feed(turns)
                    try:
                        live_sum = self.report_builder.summary_so_far()
                        sum_path = self.out_dir / "live_summary.json"
                        sum_path.write_text(json.dumps(live_sum, indent=2), encoding="utf-8")
                    except Exception as se:
                        logger.debug(f"Live summary update: {se}")
            except Exception as e:
                logger.warning(f"Error feeding report builder: {e}")

    def _forward_to_backend(self, ev: Dict[str, Any]) -> None:
        import urllib.request
        def _send():
            try:
                req = urllib.request.Request(
                    f"{self.backend_url}/meetings/{self.meeting_id}/events",
                    data=json.dumps(ev).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=1.5):
                    pass
            except Exception:
                pass
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _send)
        except Exception:
            _send()

    async def _finalize_run(self) -> None:
        """Gracefully end session, build analytics and write final report."""
        self.is_running = False
        logger.info("Finalizing session...")

        # Record call_ended
        self.store.record_event({
            "type": "call_ended",
            "meeting_id": self.meeting_id,
            "source": "runner",
            "confidence": "high"
        })

        # Close browser
        if self.browser:
            await self.browser.close()

        # Compute analytics & participation markdown
        analytics = MeetingAnalytics.from_jsonl(self.store.jsonl_path)
        stats = analytics.compute_stats()
        part_md = analytics.render_markdown_table()

        # Save stats.json
        stats_file = self.out_dir / "stats.json"
        stats_file.write_text(json.dumps(stats, indent=2), encoding="utf-8")

        # Save participation.md
        part_file = self.out_dir / "participation.md"
        part_file.write_text(part_md, encoding="utf-8")

        # Generate final report using token-budgeted ReportBuilder (WP8)
        meta = {
            "meeting_id": self.meeting_id,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "duration": stats.get("duration_fmt", "N/A"),
            "participants": f"{stats.get('total_unique_participants', 0)} ({stats.get('total_connections', 0)} connections)"
        }
        if self.report_builder:
            try:
                await self._feed_report_builder()
                final_report_md = self.report_builder.finalize(meta=meta, participation_md=part_md)
                report_file = self.out_dir / "final_report.md"
                report_file.write_text(final_report_md, encoding="utf-8")
                logger.info(f"Final report saved to {report_file}")
            except Exception as e:
                logger.error(f"Error finalizing report: {e}")
        else:
            logger.info("No LLM API key configured; skipping AI report generation. Telemetry and analytics saved.")

        self.store.close()
        self._update_status_file()
        logger.info(f"Session output complete in {self.out_dir}")

    def stop(self) -> None:
        self.stop_requested = True


def main():
    parser = argparse.ArgumentParser(description="Google Meet Automated Intelligence Bot")
    parser.add_argument("url", nargs="?", default="", help="Google Meet URL")
    parser.add_argument("--name", default="Notes Bot", help="Bot display name")
    parser.add_argument("--out-dir", default="./session_output", help="Directory for events and reports")
    parser.add_argument("--profile", default="./bot-profile", help="Persistent browser profile")
    parser.add_argument("--backend", default="", help="Backend URL to forward events")
    parser.add_argument("--max-duration", type=int, default=180, help="Max call duration in minutes")
    parser.add_argument("--headless", action="store_true", default=False, help="Run browser in headless mode (default: visible)")
    parser.add_argument("--chrome", action="store_true", help="Use installed Google Chrome")
    parser.add_argument("--login", action="store_true", help="Launch browser for manual login and exit")
    parser.add_argument("--no-chat", action="store_true", help="Do not send consent message in chat")
    args = parser.parse_args()

    if args.login:
        async def do_login():
            print("[STATUS] Launching browser for Google sign-in...", flush=True)
            browser = ControlledBrowser(profile_dir=args.profile, headless=False, use_chrome=args.chrome)
            await browser.launch()
            print("[STATUS] Opening Google sign-in page...", flush=True)
            await browser.page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
            print("Please sign into your Google account in the browser window.", flush=True)
            print("When you are finished, close the browser window.\n", flush=True)
            while browser.context and browser.context.pages:
                try:
                    await asyncio.sleep(1)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    break
            await browser.close()
            print("[STATUS] Sign-in complete. Profile saved to:", args.profile, flush=True)
        asyncio.run(do_login())
        return

    if not args.url:
        print("[ERROR] Please provide a Google Meet URL, or use --login to sign in.", flush=True)
        sys.exit(1)

    runner = BotRunner(
        url=args.url,
        name=args.name,
        out_dir=args.out_dir,
        profile_dir=args.profile,
        max_duration_min=args.max_duration,
        headless=args.headless,
        use_chrome=args.chrome,
        backend_url=args.backend,
        enable_chat=not args.no_chat,
    )

    # Attach signal handler for graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal():
        logger.info("Shutdown signal received. Stopping bot...")
        runner.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        loop.run_until_complete(runner.start())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
