"""
Meet bot: joins a Google Meet link in a real browser and records captions, speaker names and
camera/mic activity by running the SAME scraper the Chrome extension uses (extension/meet-core.js).

Output: one JSON object per line (JSONL) in the same event schema the extension stores, so a
later step (summarizer, dashboard, API) can treat extension and bot data identically.

Usage
  pip install -r requirements.txt && playwright install chromium
  python bot.py --login                       # once: sign the bot's Google account in, then close
  python bot.py https://meet.google.com/abc-defg-hij --name "Notes Bot"
  xvfb-run -a python bot.py <link>            # on a server without a display

Status: written against Meet's current structure but NOT yet verified on a live call. Expect to
adjust selectors (here or in meet-core.js CFG) after the first real run.
"""
import argparse
import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

CORE_JS = Path(__file__).resolve().parent.parent / "extension" / "meet-core.js"
LEAVE_BTN = 'button[aria-label*="Leave call" i]'
LAUNCH_ARGS = [
    "--use-fake-ui-for-media-stream",       # auto-accept mic/camera prompts
    "--use-fake-device-for-media-stream",   # fake devices so the bot sends nothing real
    "--disable-blink-features=AutomationControlled",
]


async def launch(p, profile: str, headless: bool, use_chrome: bool):
    return await p.chromium.launch_persistent_context(
        profile,
        channel="chrome" if use_chrome else None,
        headless=headless,
        args=LAUNCH_ARGS,
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1920, "height": 1080} if sys.platform.startswith("linux") else None,
    )


async def login(args):
    """Open a visible browser so you can sign the bot account in by hand, then persist the profile."""
    async with async_playwright() as p:
        ctx = await launch(p, args.profile, False, args.chrome)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://accounts.google.com/")
        print("Sign in to the bot's Google account in the browser window, then press Enter here.")
        await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        await ctx.close()


async def prejoin(page, name: str):
    """Handle the pre-join screen: guest name, mic/camera off, then Ask to join / Join now."""
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(3000)

    name_box = page.locator('input[aria-label*="name" i]').first
    if await name_box.count():
        await name_box.fill(name)

    for label in (r"turn off microphone", r"turn off camera"):
        btn = page.get_by_role("button", name=re.compile(label, re.I)).first
        if await btn.count():
            try:
                await btn.click(timeout=2000)
            except PWTimeout:
                pass

    join = page.get_by_role("button", name=re.compile(r"ask to join|join now", re.I)).first
    await join.click(timeout=15000)


async def run(args):
    out = open(args.out, "a", encoding="utf-8")
    call_ended = asyncio.Event()
    counts = {"caption": 0, "events": 0}

    def on_event(ev):  # called from the page via window.__emit
        out.write(json.dumps(ev, ensure_ascii=False) + "\n")
        out.flush()
        if args.backend:
            try:
                mid = ev.get("meetingId", "demo")
                req = urllib.request.Request(
                    f"{args.backend.rstrip('/')}/meetings/{mid}/events",
                    data=json.dumps(ev).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=1.5)
            except Exception:
                pass
        counts["events"] += 1
        if ev.get("type") == "caption":
            counts["caption"] += 1
            if ev.get("final"):
                print(f"[{ev['speaker']}] {ev['text'][:100]}")
        elif ev.get("type") in ("participant_join", "participant_leave", "call_started", "call_ended"):
            print(f"-- {ev['type']} {ev.get('name', '')}".strip())
        if ev.get("type") == "call_ended":
            call_ended.set()

    async with async_playwright() as p:
        ctx = await launch(p, args.profile, args.headless, args.chrome)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.expose_function("__emit", on_event)

        await page.goto(args.url)
        await prejoin(page, args.name)

        print("Waiting to be admitted by the host...")
        try:
            await page.wait_for_selector(LEAVE_BTN, timeout=args.admit_minutes * 60_000)
        except PWTimeout:
            print("Not admitted in time. Exiting.")
            await ctx.close()
            return

        # page.evaluate is not subject to Meet's CSP, unlike injecting a <script> tag.
        await page.evaluate(CORE_JS.read_text(encoding="utf-8"))
        await page.evaluate("() => window.MeetCore.start((ev) => window.__emit(ev))")
        print("Recording. Press Ctrl+C to stop.")

        try:
            await asyncio.wait_for(call_ended.wait(), timeout=args.max_minutes * 60)
        except asyncio.TimeoutError:
            print("Max duration reached, leaving.")
        except KeyboardInterrupt:
            pass

        try:
            await page.locator(LEAVE_BTN).first.click(timeout=3000)
        except Exception:
            pass
        await ctx.close()

    out.close()
    print(f"Done. {counts['events']} events ({counts['caption']} caption updates) written to {args.out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="Google Meet link")
    ap.add_argument("--name", default="Notes Bot", help="display name when joining as a guest")
    ap.add_argument("--profile", default="./bot-profile", help="persistent browser profile (holds the Google login)")
    ap.add_argument("--out", default="session.jsonl", help="output JSONL file")
    ap.add_argument("--backend", default="", help="optional FastAPI backend URL (e.g. http://localhost:8000)")
    ap.add_argument("--headless", action="store_true", help="hide the browser (Meet may refuse headless; prefer xvfb-run)")
    ap.add_argument("--chrome", action="store_true", help="use installed Google Chrome instead of bundled Chromium")
    ap.add_argument("--login", action="store_true", help="sign the bot's Google account in once, then exit")
    ap.add_argument("--admit-minutes", type=int, default=10, help="how long to wait in the lobby")
    ap.add_argument("--max-minutes", type=int, default=180, help="hard stop")
    args = ap.parse_args()

    if args.login:
        asyncio.run(login(args))
    elif args.url:
        try:
            asyncio.run(run(args))
        except KeyboardInterrupt:
            print("Stopped.")
    else:
        ap.error("give a Meet link, or use --login")


if __name__ == "__main__":
    main()
