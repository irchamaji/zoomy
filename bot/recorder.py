import asyncio
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlparse

import randomname

from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/recordings"))
GUEST_NAME = os.environ.get("GUEST_NAME", "Zoomy")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

Callback = Callable[..., Awaitable[None]]


def parse_zoom_url(url: str) -> tuple[str, str]:
    match = re.search(r"/j/(\d+)", url)
    if not match:
        raise ValueError(f"No meeting ID found in: {url}")
    meeting_id = match.group(1)
    params = parse_qs(urlparse(url).query)
    password = params.get("pwd", [""])[0]
    return meeting_id, password


def build_web_client_url(meeting_id: str, password: str) -> str:
    url = f"https://zoom.us/wc/{meeting_id}/join"
    if password:
        url += f"?pwd={password}"
    return url


# ── Display pool ──────────────────────────────────────────────────────────────

class DisplayPool:
    def __init__(self, base: int = 99, max_slots: int = 5):
        self._base = base
        self._max = max_slots
        self._lock = asyncio.Lock()
        self._procs: dict[int, subprocess.Popen] = {}  # display_num → Xvfb proc

    async def _ensure_pulseaudio(self) -> None:
        """Restart PulseAudio if it's not responding."""
        check = subprocess.run(["pactl", "info"], capture_output=True)
        if check.returncode != 0:
            logger.warning("PulseAudio not responding — restarting...")
            subprocess.run(
                ["pulseaudio", "--start", "--exit-idle-time=-1", "--log-level=error"],
                capture_output=True,
            )
            await asyncio.sleep(2)
            logger.info("PulseAudio restarted")

    async def acquire(self) -> tuple[int, str, str]:
        """Allocate a display slot. Returns (display_num, ':N', 'virtual_N.monitor')."""
        async with self._lock:
            await self._ensure_pulseaudio()
            for n in range(self._base, self._base + self._max):
                if n not in self._procs:
                    lock_file = f"/tmp/.X{n}-lock"
                    if os.path.exists(lock_file):
                        os.remove(lock_file)
                    proc = subprocess.Popen(
                        [
                            "Xvfb", f":{n}",
                            "-screen", "0", "1920x1080x24",
                            "-ac", "+extension", "GLX", "+render", "-noreset",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    await asyncio.sleep(1)
                    sink = f"virtual_{n}"
                    try:
                        subprocess.run(
                            [
                                "pactl", "load-module", "module-null-sink",
                                f"sink_name={sink}",
                                f"sink_properties=device.description={sink}",
                            ],
                            check=True,
                            capture_output=True,
                        )
                    except subprocess.CalledProcessError as e:
                        proc.terminate()
                        raise RuntimeError(f"Failed to create audio sink: {e.stderr.decode().strip()}") from e
                    self._procs[n] = proc
                    logger.info("DisplayPool: allocated :%d / %s", n, sink)
                    return n, f":{n}", f"{sink}.monitor"
            raise RuntimeError(f"No available display slots (max {self._max})")

    async def release(self, display_num: int) -> None:
        """Free a display slot and clean up Xvfb + PulseAudio sink."""
        async with self._lock:
            proc = self._procs.pop(display_num, None)
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            sink = f"virtual_{display_num}"
            try:
                result = subprocess.run(
                    ["pactl", "list", "short", "modules"],
                    capture_output=True, text=True,
                )
                for line in result.stdout.splitlines():
                    if sink in line:
                        subprocess.run(["pactl", "unload-module", line.split()[0]])
                        break
            except Exception as e:
                logger.warning("Could not unload sink %s: %s", sink, e)
            logger.info("DisplayPool: released :%d", display_num)


_display_pool = DisplayPool()


# ── ZoomRecorder ──────────────────────────────────────────────────────────────

class ZoomRecorder:
    def __init__(
        self,
        display: str = ":99",
        sink: str = "virtual.monitor",
        guest_name: str = GUEST_NAME,
        recording_prefix: Optional[str] = None,
        resolution: str = "1080p",
        on_started: Optional[Callback] = None,
        on_stopped: Optional[Callback] = None,
        on_error: Optional[Callback] = None,
    ):
        self.display = display
        self.sink = sink
        self.guest_name = guest_name
        self.recording_prefix = recording_prefix
        self.resolution = resolution
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.on_error = on_error
        self.is_recording = False
        self.current_url: Optional[str] = None
        self.output_path: Optional[Path] = None
        self._start_time: Optional[float] = None
        self._auto_ended = False
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._stop_event = asyncio.Event()

    def elapsed_str(self) -> str:
        if self._start_time is None:
            return "0m00s"
        elapsed = int(time.monotonic() - self._start_time)
        m, s = divmod(elapsed, 60)
        return f"{m}m{s:02d}s"

    def file_size_str(self) -> str:
        try:
            if self.output_path and self.output_path.exists():
                size = self.output_path.stat().st_size
                if size >= 1_073_741_824:
                    return f"{size / 1_073_741_824:.1f} GB"
                if size >= 1_048_576:
                    return f"{size / 1_048_576:.1f} MB"
                return f"{size / 1024:.1f} KB"
        except Exception:
            pass
        return "—"

    async def record(self, url: str) -> None:
        self.is_recording = True
        self.current_url = url
        self._stop_event.clear()
        self._start_time = time.monotonic()

        try:
            meeting_id, password = parse_zoom_url(url)
            web_url = build_web_client_url(meeting_id, password)

            RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            if not self.recording_prefix:
                self.recording_prefix = randomname.get_name()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = f"{self.recording_prefix}_{timestamp}_{meeting_id}"
            self.output_path = RECORDINGS_DIR / f"{stem}.mp4"

            logger.info("Joining %s (display=%s)", web_url, self.display)
            logger.info("Output: %s", self.output_path)

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=False,
                    env={**os.environ, "DISPLAY": self.display},
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--use-fake-ui-for-media-stream",
                        "--use-fake-device-for-media-stream",
                        "--autoplay-policy=no-user-gesture-required",
                        "--disable-blink-features=AutomationControlled",
                        "--start-maximized",
                        "--window-size=1920,1080",
                    ],
                )
                ctx = await browser.new_context(
                    no_viewport=True,
                    permissions=["microphone", "camera"],
                )
                page = await ctx.new_page()

                await self._join_meeting(page, web_url)

                self._ffmpeg = self._start_ffmpeg(str(self.output_path))
                logger.info("FFmpeg started → %s", self.output_path)

                if self.on_started:
                    await self.on_started(self.output_path.name)

                await self._watch_meeting(page)
                self._auto_ended = not self._stop_event.is_set()
                await browser.close()

        except Exception as exc:
            logger.exception("Recorder failed")
            if self.on_error:
                await self.on_error(str(exc))
        finally:
            elapsed = int(time.monotonic() - self._start_time)
            self._stop_ffmpeg()
            self.is_recording = False
            self.current_url = None

            if self.output_path and self.on_stopped:
                m, s = divmod(elapsed, 60)
                duration_str = f"{m}m{s:02d}s"
                size_str = self.file_size_str()
                reason = "host ended" if self._auto_ended else "manual stop"
                logger.info(
                    "Recording done — %s | duration=%s | size=%s | reason=%s",
                    self.output_path.name, duration_str, size_str, reason,
                )
                await self.on_stopped(
                    self.output_path.name, duration_str, size_str, self._auto_ended,
                )

    async def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------

    async def _screenshot(self, page: Page, label: str) -> None:
        if not DEBUG:
            return
        try:
            path = RECORDINGS_DIR / f"debug_{label}.png"
            await page.screenshot(path=str(path), full_page=False)
            logger.info("Screenshot: %s", path.name)
        except Exception as e:
            logger.warning("Screenshot failed (%s): %s", label, e)

    async def _dismiss_notifications(self, page: Page) -> None:
        try:
            await page.evaluate("""
                document.querySelectorAll(
                    '[class*="notification"] button, [class*="banner"] button, [class*="alert"] button'
                ).forEach(btn => {
                    const txt = (btn.textContent || btn.getAttribute('aria-label') || '');
                    if (txt.includes('×') || txt.includes('✕') || txt.toLowerCase().includes('close') || txt === 'x') {
                        btn.click();
                    }
                });
            """)
        except Exception:
            pass
        try:
            close_btns = page.locator(
                '[class*="notification"] button, [class*="banner"] button'
            )
            count = await close_btns.count()
            for i in range(count):
                try:
                    btn = close_btns.nth(i)
                    if await btn.is_visible():
                        await btn.click(timeout=1_000)
                except Exception:
                    pass
        except Exception:
            pass

    async def _join_meeting(self, page: Page, web_url: str) -> None:
        await page.goto(web_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)
        await self._screenshot(page, "01_initial")

        for _ in range(4):
            try:
                link = page.get_by_text("Join from Your Browser", exact=False)
                if await link.count() > 0:
                    await link.first.click()
                    await asyncio.sleep(2)
                    break
            except Exception:
                pass
            await asyncio.sleep(2)

        NAME_SELECTORS = [
            'input[placeholder="Your Name"]',
            'input[placeholder="your name"]',
            'input#inputname',
            'input[data-testid*="name"]',
            'input[class*="name" i]',
            'input[type="text"]',
        ]
        name_filled = False
        for sel in NAME_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.clear()
                    await el.fill(self.guest_name)
                    name_filled = True
                    logger.info("Name filled using: %s", sel)
                    break
            except Exception:
                continue
        if not name_filled:
            logger.warning("Name field not found")

        for label in ["Mute", "Stop Video"]:
            try:
                btn = page.get_by_text(label, exact=True)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(timeout=2_000)
                    logger.info("Pre-join clicked: %s", label)
            except Exception:
                pass
        await self._screenshot(page, "02_name_filled")

        JOIN_SELECTORS = [
            'button.preview-join-button',
            'button[type="submit"]',
            'input[type="submit"]',
            'button[class*="join" i]',
        ]
        join_clicked = False
        for sel in JOIN_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click(timeout=5_000)
                    join_clicked = True
                    logger.info("Join clicked: %s", sel)
                    break
            except Exception:
                continue
        if not join_clicked:
            try:
                btn = page.get_by_role("button", name=re.compile(r"join", re.I))
                await btn.first.click(timeout=5_000)
                join_clicked = True
                logger.info("Join clicked via role fallback")
            except Exception:
                logger.warning("Join button not found")

        try:
            await page.wait_for_url(
                lambda url: "/join" not in url,
                timeout=30_000,
            )
            logger.info("Entered meeting. URL: %s", page.url)
        except Exception:
            logger.warning("Still on join page — URL: %s", page.url)

        try:
            audio_btn = page.get_by_text("Join Audio by Computer", exact=False)
            if await audio_btn.count() > 0:
                await audio_btn.first.click(timeout=5_000)
        except Exception:
            pass

        await asyncio.sleep(3)

        await self._dismiss_notifications(page)
        await asyncio.sleep(1)
        await self._dismiss_notifications(page)

        await self._screenshot(page, "03_in_meeting")

    async def _watch_meeting(self, page: Page) -> None:
        ended_substrings = [
            "been ended by host",
            "been ended by the host",
            "meeting has ended",
            "meeting is ended",
            "host has ended",
            "removed from this meeting",
        ]
        while not self._stop_event.is_set():
            try:
                # FFmpeg health check — if process exited, encoding stopped
                if self._ffmpeg and self._ffmpeg.poll() is not None:
                    logger.error("FFmpeg exited unexpectedly (code %d)", self._ffmpeg.returncode)
                    try:
                        if self.on_error:
                            await self.on_error(f"FFmpeg stopped unexpectedly (code {self._ffmpeg.returncode})")
                    except Exception:
                        logger.exception("on_error callback failed after FFmpeg exit")
                    return

                if page.is_closed():
                    logger.info("Page closed — stopping")
                    return
                visible_text = (await page.evaluate("document.body.innerText")).lower()
                for phrase in ended_substrings:
                    if phrase in visible_text:
                        logger.info("Meeting ended detected: '%s'", phrase)
                        return
                if "zoom.us/wc" not in page.url:
                    logger.info("Left meeting URL: %s", page.url)
                    return
            except Exception:
                pass
            await asyncio.sleep(3)

    _RESOLUTION_SCALE = {
        "360p": "640:360",
        "720p": "1280:720",
        "1080p": None,
    }

    def _start_ffmpeg(self, output: str) -> subprocess.Popen:
        scale = self._RESOLUTION_SCALE.get(self.resolution)
        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "warning", "-nostats",
            "-f", "x11grab", "-r", "25", "-s", "1920x1080", "-i", self.display,
            "-f", "pulse", "-i", self.sink,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p",
        ]
        if scale:
            cmd += ["-vf", f"scale={scale}"]
        cmd += [
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output,
        ]
        # Write FFmpeg output to logs dir (avoids PIPE buffer blocking)
        log_dir = RECORDINGS_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / (Path(output).stem + ".ffmpeg.log")
        ffmpeg_log = open(log_path, "w", encoding="utf-8")
        logger.info("FFmpeg log → logs/%s", log_path.name)
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=ffmpeg_log,
            stderr=ffmpeg_log,
        )

    def _stop_ffmpeg(self) -> None:
        if not self._ffmpeg:
            return
        if self._ffmpeg.poll() is None:
            try:
                self._ffmpeg.stdin.write(b"q")
                self._ffmpeg.stdin.flush()
                self._ffmpeg.wait(timeout=15)
            except (subprocess.TimeoutExpired, BrokenPipeError, OSError):
                self._ffmpeg.kill()
        self._ffmpeg = None
        logger.info("FFmpeg stopped")
