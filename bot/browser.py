"""
browser.py - Controlled Playwright browser, viewport, layout control and panel management (WP1 & WP2).

Enforces non-throttled foreground execution, fixed large viewport, Tiled 49-tile layout,
and keeping the People panel open for accurate roster observation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("MeetBot.Browser")

DEFAULT_LAUNCH_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--mute-audio",
    "--no-audio",
    "--disable-audio-output",
    "--autoplay-policy=no-user-gesture-required",
    "--lang=hi-IN,en-IN,en-US",
]


class ControlledBrowser:
    def __init__(
        self,
        profile_dir: str = "./bot-profile",
        headless: bool = False,
        use_chrome: bool = False,
        viewport_width: Optional[int] = None,
        viewport_height: Optional[int] = None,
        selectors_path: Optional[str | Path] = None,
        out_dir: Optional[str | Path] = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.use_chrome = use_chrome
        self.out_dir = Path(out_dir) if out_dir else Path("./session_output")

        # When running locally on Windows, do not force 1920x1080.
        # Use None (real screen size). Keep 1920x1080 only for Linux server mode.
        if viewport_width and viewport_height:
            self.viewport = {"width": viewport_width, "height": viewport_height}
        elif sys.platform.startswith("linux") and (os.environ.get("HEADLESS_SERVER") or headless or not os.environ.get("DISPLAY")):
            self.viewport = {"width": 1920, "height": 1080}
        else:
            self.viewport = None

        self.selectors = self._load_selectors(selectors_path)
        self._playwright = None
        self.context = None
        self.page = None

    def _load_selectors(self, path: Optional[str | Path]) -> Dict[str, Any]:
        if path and Path(path).exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        default_path = Path(__file__).parent / "selectors.json"
        if default_path.exists():
            with open(default_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    async def capture_debug_screenshot(self, reason: str, out_dir: Optional[Path] = None) -> Optional[Path]:
        """Save a screenshot on any failure and print the reason."""
        target_dir = out_dir or self.out_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / "debug.png"
        print(f"[ERROR] Failure: {reason}", flush=True)
        logger.error(f"Failure: {reason}")
        if self.page:
            try:
                await self.page.screenshot(path=str(dest), full_page=False)
                print(f"[DEBUG] Screenshot saved to: {dest}", flush=True)
                logger.info(f"Debug screenshot saved to {dest}")
                return dest
            except Exception as e:
                logger.warning(f"Could not take debug screenshot: {e}")
        return None

    async def launch(self) -> None:
        """Launch the persistent browser context."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        channel = "chrome" if self.use_chrome else None
        logger.info(
            f"Launching browser (chrome={self.use_chrome}, headless={self.headless}, viewport={self.viewport})"
        )

        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            channel=channel,
            viewport=self.viewport,
            locale="hi-IN",
            permissions=["microphone", "camera"],
            args=DEFAULT_LAUNCH_ARGS,
        )

        # Permanently silence all WebRTC and HTML audio in the bot context so it cannot echo on user's speakers
        await self.context.add_init_script("""
(() => {
    const silence = (el) => {
        try {
            el.muted = true;
            el.volume = 0;
            el.setAttribute('muted', '');
        } catch (_) {}
    };
    try {
        const origPlay = HTMLMediaElement.prototype.play;
        HTMLMediaElement.prototype.play = function() {
            silence(this);
            return origPlay.apply(this, arguments);
        };
        const obs = new MutationObserver((mutations) => {
            for (const m of mutations) {
                for (const n of m.addedNodes) {
                    if (n instanceof HTMLMediaElement) silence(n);
                    if (n && n.querySelectorAll) n.querySelectorAll('audio, video').forEach(silence);
                }
            }
        });
        obs.observe(document.documentElement || document, { childList: true, subtree: true });
        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('audio, video').forEach(silence);
        });
    } catch (_) {}

    try {
        if (typeof AudioContext !== 'undefined') {
            const origResume = AudioContext.prototype.resume;
            AudioContext.prototype.resume = function() {
                return Promise.resolve();
            };
        }
    } catch (_) {}
})()
""")

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def join_meeting(
        self,
        url: str,
        name: str = "Notes Bot",
        max_lobby_wait_sec: int = 300,
        out_dir: Optional[Path] = None,
    ) -> bool:
        """Navigate, disable mic/cam, join or ask to join, and wait through lobby."""
        if not self.page:
            raise RuntimeError("Browser not launched. Call launch() first.")

        save_dir = out_dir or self.out_dir

        print(f"[STATUS] Opening link: {url}", flush=True)
        logger.info(f"Navigating to {url}")
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
        except Exception as e:
            await self.capture_debug_screenshot(f"Failed to navigate to {url}: {e}", save_dir)
            return False

        print("[STATUS] Pre-join screen: configuring name, mic off, camera off...", flush=True)

        # Dismiss any initial helper popups
        for dismiss_sel in [
            "button:has-text('Dismiss')",
            "button:has-text('Got it')",
            "button:has-text('Continue without microphone')",
            "button:has-text('Continue without camera')",
        ]:
            try:
                btn = await self.page.query_selector(dismiss_sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.5)
            except Exception:
                pass

        # 1. Fill guest name if present
        name_sel = self.selectors.get("lobby", {}).get(
            "guest_name_input", "input[type='text'], input[aria-label*='name' i]"
        )
        try:
            name_input = await self.page.wait_for_selector(name_sel, timeout=4000)
            if name_input and await name_input.is_visible():
                await name_input.fill(name)
                await self.page.keyboard.press("Tab")
                logger.info(f"Filled guest name: {name}")
                await asyncio.sleep(0.5)
        except Exception:
            pass

        # 2. Mute microphone and turn off camera before joining
        await self._pre_join_mute_devices()

        # 3. Click Join / Ask to join / Switch here / Join here too
        join_btn = None

        # Check if 'Other ways to join' is visible (allows 'Join here too' without disconnecting host)
        try:
            other_ways = await self.page.query_selector(
                "button:has-text('Other ways to join'), [role='button']:has-text('Other ways to join')"
            )
            if other_ways and await other_ways.is_visible():
                await other_ways.click()
                await asyncio.sleep(0.5)
                join_too = await self.page.query_selector(
                    "button:has-text('Join here too'), [role='menuitem']:has-text('Join here too'), [role='button']:has-text('Join here too'), span:has-text('Join here too')"
                )
                if join_too and await join_too.is_visible():
                    join_btn = join_too
                    logger.info("Found and selected 'Join here too' option")
        except Exception:
            pass

        if not join_btn:
            join_selectors = [
                "button:has-text('Join now')",
                "button:has-text('Ask to join')",
                "button:has-text('Join here too')",
                "button:has-text('Switch here')",
                "button:has-text('Join')",
                "button:has-text('Rejoin')",
                "button[aria-label*='Join now' i]",
                "button[aria-label*='Ask to join' i]",
                "button[aria-label*='Switch here' i]",
                "button[aria-label*='Join' i]",
                "[role='button']:has-text('Join now')",
                "[role='button']:has-text('Ask to join')",
                "[role='button']:has-text('Join here too')",
                "[role='button']:has-text('Switch here')",
                "[role='button']:has-text('Join')",
                "span:has-text('Join now')",
                "span:has-text('Ask to join')",
                "span:has-text('Join here too')",
                "span:has-text('Switch here')",
            ]
            for sel in join_selectors:
                try:
                    elem = await self.page.wait_for_selector(sel, timeout=2000)
                    if elem and await elem.is_visible():
                        join_btn = elem
                        break
                except Exception:
                    continue

        if join_btn:
            try:
                await join_btn.click()
                logger.info("Clicked join button")
                await asyncio.sleep(1)

                # Check if a confirmation dialog appeared (e.g., Switch here confirmation)
                for confirm_sel in [
                    "[role='dialog'] button:has-text('Switch here')",
                    "[role='dialog'] button:has-text('Switch')",
                    "[role='dialog'] button:has-text('Join')",
                ]:
                    try:
                        cbtn = await self.page.query_selector(confirm_sel)
                        if cbtn and await cbtn.is_visible():
                            await cbtn.click()
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Error clicking join button: {e}")
        else:
            # Check if we are already in the call
            leave_sel = self.selectors.get("controls", {}).get(
                "leave_call", "button[aria-label*='Leave call' i], button[aria-label*='Leave' i]"
            )
            already_in = await self.page.query_selector(leave_sel)
            if not (already_in and await already_in.is_visible()):
                await self.capture_debug_screenshot("Could not find join button on pre-join screen", save_dir)
                return False

        # 4. Wait through lobby / admission
        print("[STATUS] Waiting to be admitted into the call...", flush=True)
        in_call = await self._wait_for_call_admission(max_lobby_wait_sec, save_dir)
        if not in_call:
            return False

        print("[STATUS] In the call! Connected successfully.", flush=True)
        logger.info("Successfully entered meeting")

        # Dismiss post-join modal popups / scrims (e.g. "Your meeting is ready", "Add others")
        await self.dismiss_popups()

        # Turn on captions safely (try forced click + keyboard 'c' fallback)
        await self.ensure_captions_on()

        # 5. Apply layout & panels post-join (WP2)
        await self.apply_tiled_layout_and_people_panel()
        return True

    async def dismiss_popups(self) -> None:
        """Dismiss overlay dialogs like 'Your meeting is ready', 'Add others', 'Got it'."""
        if not self.page:
            return
        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass
        for sel in [
            "[role='dialog'] button[aria-label*='close' i]",
            "[role='dialog'] button[aria-label*='dismiss' i]",
            "button[aria-label*='close' i]",
            "button:has-text('Got it')",
            "button:has-text('Dismiss')",
        ]:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=1000, force=True)
                    await asyncio.sleep(0.2)
            except Exception:
                pass

    async def ensure_captions_on(self) -> None:
        """Ensure captions are active in the meeting."""
        if not self.page:
            return
        await self.dismiss_popups()
        off_sel = "button[aria-label*='turn off captions' i], button[aria-label*='turn off live captions' i]"
        try:
            off_btn = await self.page.query_selector(off_sel)
            if off_btn and await off_btn.is_visible():
                logger.info("Captions are already active.")
                await self.configure_caption_language("Hindi")
                return
        except Exception:
            pass

        # Check if caption region is already visible in the DOM
        try:
            region = await self.page.query_selector("div[role='region'][aria-label*='aption' i], div[aria-label*='caption' i]")
            if region and await region.is_visible():
                logger.info("Caption region is already active.")
                await self.configure_caption_language("Hindi")
                return
        except Exception:
            pass

        # Try pressing 'c' key directly first - fast and immune to button occlusion
        try:
            await self.page.keyboard.press("c")
            await asyncio.sleep(0.5)
            off_btn = await self.page.query_selector(off_sel)
            if off_btn and await off_btn.is_visible():
                logger.info("Captions enabled via shortcut 'c'.")
                await self.configure_caption_language("Hindi")
                return
        except Exception:
            pass

        # Try finding button and clicking with force and short timeout
        on_sel = "button[aria-label*='turn on captions' i], button[aria-label*='turn on live captions' i]"
        try:
            on_btn = await self.page.query_selector(on_sel)
            if on_btn and await on_btn.is_visible():
                await on_btn.click(force=True, timeout=2000)
                logger.info("Clicked 'Turn on captions' button (forced).")
                await asyncio.sleep(0.5)
            else:
                await self.page.keyboard.press("c")
        except Exception as e:
            logger.warning(f"Button click failed, pressing 'c': {e}")
            try:
                await self.page.keyboard.press("c")
            except Exception:
                pass

        await asyncio.sleep(0.5)
        await self.configure_caption_language("Hindi")

    async def configure_caption_language(self, target_lang: str = "Hindi") -> None:
        """Attempt to configure Google Meet caption language to Hindi/Hinglish if picker is shown."""
        if not self.page:
            return
        try:
            # 1. Look for caption language selector/button near bottom bar
            lang_btn_sels = [
                "button[aria-label*='caption language' i]",
                "button[aria-label*='language' i][aria-label*='caption' i]",
                "button[aria-label*='caption settings' i]",
                "[role='button'][aria-label*='caption language' i]",
            ]
            for sel in lang_btn_sels:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click(force=True, timeout=1000)
                    await asyncio.sleep(0.5)
                    break

            # 2. Search for Hindi option in dialog / dropdown
            hindi_sels = [
                f"[role='option']:has-text('{target_lang}')",
                f"[role='radio']:has-text('{target_lang}')",
                "[role='option']:has-text('हिन्दी')",
                "[role='radio']:has-text('हिन्दी')",
                f"button:has-text('{target_lang}')",
                "button:has-text('हिन्दी')",
                f"span:has-text('{target_lang}')",
            ]
            for h_sel in hindi_sels:
                h_opt = await self.page.query_selector(h_sel)
                if h_opt and await h_opt.is_visible():
                    await h_opt.click(force=True, timeout=1000)
                    logger.info(f"Configured Meet caption language to: {target_lang}")
                    await asyncio.sleep(0.5)
                    # Look for Apply or Done button
                    for apply_sel in ["button:has-text('Apply')", "button:has-text('Save')", "button:has-text('Done')", "[role='button']:has-text('Apply')"]:
                        apply_btn = await self.page.query_selector(apply_sel)
                        if apply_btn and await apply_btn.is_visible():
                            await apply_btn.click(timeout=1000)
                            await asyncio.sleep(0.3)
                    await self.dismiss_popups()
                    return
        except Exception as e:
            logger.debug(f"Caption language autodetection completed: {e}")

    async def _pre_join_mute_devices(self) -> None:
        """Ensure mic and camera are toggled off before entry."""
        if not self.page:
            return
        # Find mic button
        mic_sel = self.selectors.get("controls", {}).get(
            "mic_button", "button[aria-label*='microphone' i], button[aria-label*='mic' i]"
        )
        try:
            btns = await self.page.query_selector_all(mic_sel)
            for b in btns:
                aria = (await b.get_attribute("aria-label") or "").lower()
                muted = await b.get_attribute("data-is-muted")
                if "turn off" in aria or muted == "false":
                    await b.click()
                    logger.info("Muted microphone pre-join")
                    break
        except Exception:
            pass

        # Find camera button
        cam_sel = self.selectors.get("controls", {}).get(
            "cam_button", "button[aria-label*='camera' i], button[aria-label*='video' i]"
        )
        try:
            btns = await self.page.query_selector_all(cam_sel)
            for b in btns:
                aria = (await b.get_attribute("aria-label") or "").lower()
                if "turn off" in aria:
                    await b.click()
                    logger.info("Turned off camera pre-join")
                    break
        except Exception:
            pass

    async def _wait_for_call_admission(self, timeout_sec: int, save_dir: Path) -> bool:
        """Wait until call controls (e.g. Leave call button) become visible."""
        if not self.page:
            return False
        leave_sel = self.selectors.get("controls", {}).get(
            "leave_call", "button[aria-label*='Leave call' i], button[aria-label*='Leave' i], button[data-tooltip*='Leave' i]"
        )
        start_time = asyncio.get_event_loop().time()
        last_log = start_time
        while (asyncio.get_event_loop().time() - start_time) < timeout_sec:
            elapsed = int(asyncio.get_event_loop().time() - start_time)

            # Check if admitted into meeting
            leave_btn = await self.page.query_selector(leave_sel)
            if leave_btn and await leave_btn.is_visible():
                return True

            # Check if denied or removed
            denied_sel = self.selectors.get("lobby", {}).get("denied_indicator")
            if denied_sel:
                try:
                    denied = await self.page.query_selector(denied_sel)
                    if denied and await denied.is_visible():
                        await self.capture_debug_screenshot("Denied entry or removed by meeting host", save_dir)
                        return False
                except Exception:
                    pass

            now = asyncio.get_event_loop().time()
            if now - last_log >= 10:
                print(f"[STATUS] Waiting to be admitted by the meeting host... ({elapsed}s elapsed)", flush=True)
                last_log = now

            await asyncio.sleep(2)

        await self.capture_debug_screenshot(f"Timed out after {timeout_sec}s waiting to be admitted into call", save_dir)
        return False

    async def apply_tiled_layout_and_people_panel(self) -> None:
        """Set layout to Tiled with max tiles (49) and open People panel (WP2)."""
        if not self.page:
            return
        await asyncio.sleep(2)

        # 1. Open People panel and keep it open
        people_btn_sel = self.selectors.get("panels", {}).get(
            "people_button", "button[aria-label*='everyone' i], button[aria-label*='people' i]"
        )
        panel_sel = self.selectors.get("panels", {}).get("people_panel", "[role='tabpanel']")
        try:
            panel = await self.page.query_selector(panel_sel)
            if not panel or not await panel.is_visible():
                people_btn = await self.page.query_selector(people_btn_sel)
                if people_btn and await people_btn.is_visible():
                    await people_btn.click()
                    logger.info("Opened People panel")
                    await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Could not open People panel: {e}")

        # 2. Open Change Layout menu -> Tiled (49 tiles)
        try:
            more_sel = self.selectors.get("controls", {}).get(
                "more_options", "button[aria-label*='more options' i], button[aria-label*='more' i]"
            )
            more_btn = await self.page.query_selector(more_sel)
            if more_btn and await more_btn.is_visible():
                await more_btn.click()
                await asyncio.sleep(0.8)

                # Look for 'Change layout'
                layout_item = await self.page.query_selector(
                    "[role='menuitem']:has-text('Change layout'), [role='menuitem'][aria-label*='layout' i]"
                )
                if layout_item and await layout_item.is_visible():
                    await layout_item.click()
                    await asyncio.sleep(0.8)

                    # Select 'Tiled'
                    tiled_opt = await self.page.query_selector(
                        "[role='radio']:has-text('Tiled'), [role='radio'][aria-label*='tiled' i]"
                    )
                    if tiled_opt and await tiled_opt.is_visible():
                        await tiled_opt.click()
                        logger.info("Selected Tiled layout")

                    # Adjust slider to max (49 tiles) if slider exists
                    slider = await self.page.query_selector("[role='slider']")
                    if slider:
                        await slider.evaluate("el => { el.value = 49; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); }")
                        logger.info("Set tile slider to 49")

                    # Close modal dialog
                    close_btn = await self.page.query_selector("button[aria-label*='close' i], button:has-text('Close')")
                    if close_btn and await close_btn.is_visible():
                        await close_btn.click()
                    else:
                        await self.page.keyboard.press("Escape")
        except Exception as e:
            logger.warning(f"Could not change layout: {e}")

    async def send_chat_message(self, message: str) -> bool:
        """Send a consent notice or status message to meeting chat (WP9)."""
        if not self.page:
            return False
        try:
            await self.dismiss_popups()
            chat_btn_sel = self.selectors.get("controls", {}).get(
                "chat_button", "button[aria-label*='chat' i]"
            )
            chat_btn = await self.page.query_selector(chat_btn_sel)
            if chat_btn and await chat_btn.is_visible():
                await chat_btn.click(force=True, timeout=3000)
                await asyncio.sleep(1)

                input_sel = self.selectors.get("controls", {}).get(
                    "chat_input", "textarea[aria-label*='chat' i], textarea[aria-label*='message' i]"
                )
                chat_input = await self.page.wait_for_selector(input_sel, timeout=5000)
                if chat_input and await chat_input.is_visible():
                    await chat_input.fill(message)
                    await self.page.keyboard.press("Enter")
                    logger.info(f"Sent chat message: {message}")
                    return True
        except Exception as e:
            logger.warning(f"Could not send chat message: {e}")
        return False

    async def close(self) -> None:
        """Gracefully close page, context and playwright."""
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
