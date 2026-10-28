"""
record.py - Calibration harness for Google Meet (WP0).

Joins a call and periodically saves:
- page.content() HTML snapshot
- full-page screenshot
- parsed scraper state & signals
into fixtures/<call>/ for offline verification.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from browser import ControlledBrowser

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MeetBot.Record")


async def run_record(
    url: str,
    output_dir: str,
    duration_sec: int = 60,
    interval_sec: int = 5,
    name: str = "Calibration Bot",
    headless: bool = False,
    chrome: bool = False,
):
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    browser = ControlledBrowser(
        profile_dir="./record-profile",
        headless=headless,
        use_chrome=chrome,
        viewport_width=1920,
        viewport_height=1080,
    )

    try:
        await browser.launch()
        logger.info(f"Joining {url} for calibration recording...")
        joined = await browser.join_meeting(url, name=name, max_lobby_wait_sec=180)
        if not joined:
            logger.error("Could not join call")
            return

        # Inject meet-core.js to collect parsed signals
        core_js = Path(__file__).parent.parent / "extension" / "meet-core.js"
        if core_js.exists():
            await browser.page.add_script_tag(content=core_js.read_text(encoding="utf-8"))

        start_time = asyncio.get_event_loop().time()
        sample_idx = 0

        logger.info(f"Recording snapshots to {out_path} every {interval_sec}s for {duration_sec}s")
        while (asyncio.get_event_loop().time() - start_time) < duration_sec:
            sample_idx += 1
            ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            sample_prefix = f"sample_{sample_idx:03d}_{ts_str}"

            # 1. Save HTML DOM
            html_content = await browser.page.content()
            html_file = out_path / f"{sample_prefix}.html"
            html_file.write_text(html_content, encoding="utf-8")

            # 2. Save screenshot
            png_file = out_path / f"{sample_prefix}.png"
            await browser.page.screenshot(path=str(png_file), full_page=False)

            # 3. Read parsed DOM signals
            try:
                parsed_data = await browser.page.evaluate("""() => {
                    const tiles = Array.from(document.querySelectorAll('[data-participant-id], [data-requested-participant-id]'));
                    const peoplePanel = Array.from(document.querySelectorAll('[role="listitem"][data-participant-id], [role="tabpanel"] [role="listitem"]'));
                    const captions = Array.from(document.querySelectorAll('div[role="region"][aria-label*="caption" i] span'));
                    return {
                        tile_count: tiles.length,
                        people_panel_items: peoplePanel.length,
                        caption_snippet_count: captions.length
                    };
                }""")
            except Exception:
                parsed_data = {}

            meta_file = out_path / f"{sample_prefix}.json"
            meta_file.write_text(json.dumps(parsed_data, indent=2), encoding="utf-8")

            logger.info(f"Saved snapshot {sample_idx} -> {sample_prefix}")
            await asyncio.sleep(interval_sec)

        logger.info("Recording complete.")
    finally:
        await browser.close()


def main():
    parser = argparse.ArgumentParser(description="Record live Google Meet DOM and screenshots")
    parser.add_argument("url", help="Google Meet URL")
    parser.add_argument("--out", default="./fixtures/sample_call", help="Directory to save snapshots")
    parser.add_argument("--duration", type=int, default=30, help="Duration in seconds")
    parser.add_argument("--interval", type=int, default=5, help="Snapshot interval in seconds")
    parser.add_argument("--name", default="Calibration Bot", help="Bot display name")
    parser.add_argument("--headless", action="store_true", help="Run browser headlessly")
    parser.add_argument("--chrome", action="store_true", help="Use installed Chrome")
    args = parser.parse_args()

    asyncio.run(
        run_record(
            url=args.url,
            output_dir=args.out,
            duration_sec=args.duration,
            interval_sec=args.interval,
            name=args.name,
            headless=args.headless,
            chrome=args.chrome,
        )
    )


if __name__ == "__main__":
    main()
