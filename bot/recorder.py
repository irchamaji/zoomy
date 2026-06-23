import asyncio
import json
import logging
import os
import re
import struct
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
GUEST_NAME = os.environ.get("GUEST_NAME", "zoomy.ircham.dev")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# ── Audio-wait settings ───────────────────────────────────────────────────────
# How long (seconds) to wait for audio before starting the recording anyway.
# Set to 0 to disable the feature and start FFmpeg immediately on join.
AUDIO_WAIT_TIMEOUT = int(os.environ.get("AUDIO_WAIT_TIMEOUT", "300"))
# Minimum RMS level (out of 32 767) considered "audio present".
AUDIO_RMS_THRESHOLD = int(os.environ.get("AUDIO_RMS_THRESHOLD", "100"))
_AUDIO_SAMPLE_RATE = 16_000   # Hz — enough for level detection, low CPU
_AUDIO_CHUNK_SECS = 0.25      # read window per iteration

# ── Silence-warning settings ──────────────────────────────────────────────────
# Seconds of continuous silence during recording before Telegram is notified.
# Set to 0 to disable.
SILENT_WARN_SECS = int(os.environ.get("SILENT_WARN_SECS", "60"))
# How long (seconds) a user-initiated snooze suppresses further warnings.
SILENT_SNOOZE_SECS = int(os.environ.get("SILENT_SNOOZE_SECS", "300"))

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


def _is_google_meet_url(url: str) -> bool:
    try:
        return "meet.google.com" in urlparse(url).netloc.lower()
    except Exception:
        return False


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
                            "-screen", "0", "1920x1200x24",
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
        meeting_password: Optional[str] = None,
        on_started: Optional[Callback] = None,
        on_stopped: Optional[Callback] = None,
        on_error: Optional[Callback] = None,
        on_dialog: Optional[Callback] = None,
        on_waiting: Optional[Callback] = None,
        on_silence_warn: Optional[Callback] = None,
        on_audio_returned: Optional[Callback] = None,
    ):
        self.display = display
        self.sink = sink
        self.guest_name = guest_name
        self.recording_prefix = recording_prefix
        self.resolution = resolution
        self.meeting_password = meeting_password
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.on_error = on_error
        self.on_dialog = on_dialog
        self.on_waiting = on_waiting
        self.on_silence_warn = on_silence_warn
        self.on_audio_returned = on_audio_returned
        # Silence-monitor state — mutated by silence_snooze() / silence_wait_for_audio()
        self._silence_state: str = "monitoring"   # monitoring | warned | snoozed | waiting
        self._snooze_until: float = 0.0
        self.is_recording = False
        self.current_url: Optional[str] = None
        self.output_path: Optional[Path] = None
        self._start_time: Optional[float] = None
        self._started_at: Optional[datetime] = None
        self._auto_ended = False
        self._ffmpeg: Optional[subprocess.Popen] = None
        self._ffmpeg_log = None
        self._stop_event = asyncio.Event()
        self._dialog_pending: bool = False
        self._session_errored: bool = False

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
        self._started_at = datetime.now()

        is_google_meet = _is_google_meet_url(url)

        try:
            if is_google_meet:
                web_url = url
            else:
                meeting_id, password = parse_zoom_url(url)
                web_url = build_web_client_url(meeting_id, password)

            if not self.recording_prefix:
                self.recording_prefix = randomname.get_name()
            datestamp = datetime.now().strftime("%Y%m%d")
            folder_stem = f"{self.recording_prefix}_{datestamp}"
            recording_dir = RECORDINGS_DIR / folder_stem
            recording_dir.mkdir(parents=True, exist_ok=True)
            self.output_path = recording_dir / f"{self.recording_prefix}.mp4"

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
                        "--window-size=1920,1200",
                    ],
                )
                ctx = await browser.new_context(
                    no_viewport=True,
                    permissions=["microphone", "camera"],
                )
                page = await ctx.new_page()

                if is_google_meet:
                    await self._join_google_meet(page, web_url)
                    chrome_height = 0  # no crop for Google Meet
                else:
                    await self._join_meeting(page, web_url)
                    # Measure browser chrome height for cropping
                    try:
                        chrome_height = await page.evaluate(
                            "window.outerHeight - window.innerHeight"
                        )
                        chrome_height = max(0, int(chrome_height))
                    except Exception:
                        chrome_height = 0
                    logger.info("Browser chrome height: %dpx", chrome_height)

                if is_google_meet:
                    async def _gmeet_guard():
                        _cant_join = ("you can't join this video call", "cannot join this video call")
                        while not self._stop_event.is_set():
                            await asyncio.sleep(3)
                            try:
                                if page.is_closed():
                                    return
                                current_url = page.url
                                if "meet.google.com" not in current_url:
                                    msg = f"Google Meet: browser redirected to {current_url!r}."
                                elif any(p in (await page.evaluate("document.body.innerText")).lower() for p in _cant_join):
                                    msg = "Google Meet: unable to join this video call. Meeting may require a signed-in Google account."
                                else:
                                    continue
                                self._session_errored = True
                                self._stop_event.set()
                                logger.warning("GMeet guard: %s", msg)
                                if self.on_error:
                                    await self.on_error(msg)
                                return
                            except Exception:
                                pass
                    guard = asyncio.create_task(_gmeet_guard())
                    try:
                        await self._wait_for_audio()
                    finally:
                        guard.cancel()
                        try:
                            await guard
                        except (asyncio.CancelledError, Exception):
                            pass
                else:
                    await self._wait_for_audio()

                if self._session_errored:
                    return

                self._ffmpeg = self._start_ffmpeg(str(self.output_path), chrome_height)
                logger.info("FFmpeg started → %s", self.output_path)

                if self.on_started:
                    await self.on_started(str(self.output_path))

                silence_task = asyncio.create_task(self._silence_monitor())
                try:
                    if is_google_meet:
                        await self._watch_google_meet(page)
                    else:
                        await self._watch_meeting(page)
                finally:
                    silence_task.cancel()
                    try:
                        await silence_task
                    except (asyncio.CancelledError, Exception):
                        pass

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

            if self.output_path and not self._session_errored:
                metadata = {
                    "recording_name": self.recording_prefix,
                    "url": url,
                    "duration_seconds": elapsed,
                    "started_at": self._started_at.isoformat() if self._started_at else None,
                }
                try:
                    meta_path = self.output_path.parent / ".metadata.json"
                    meta_path.write_text(
                        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                except Exception as e:
                    logger.warning("Could not write metadata: %s", e)

            if self.output_path and not self._session_errored and self.on_stopped:
                m, s = divmod(elapsed, 60)
                duration_str = f"{m}m{s:02d}s"
                size_str = self.file_size_str()
                reason = "host ended" if self._auto_ended else "manual stop"
                logger.info(
                    "Recording done — %s | duration=%s | size=%s | reason=%s",
                    self.output_path.parent.name, duration_str, size_str, reason,
                )
                await self.on_stopped(
                    str(self.output_path), duration_str, size_str, self._auto_ended,
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

    async def _dismiss_blocking_dialogs(self, page: Page) -> None:
        """Detect any blocking modal with an OK-type button, notify, and dismiss it."""
        if self._dialog_pending:
            return
        JS = """
            (() => {
                const OK_LABELS = ['ok', 'got it', 'i understand', 'accept', 'dismiss', 'continue', 'close'];

                // Zoom uses both <button> and [role="button"] divs/spans
                function clickables(root) {
                    return [...root.querySelectorAll('button, [role="button"]')];
                }

                // Pass 1: modal/dialog containers (including Zoom-specific classes)
                const containers = document.querySelectorAll(
                    '[role="dialog"], [role="alertdialog"], '
                    + '[class*="modal"], [class*="dialog"], [class*="overlay"], [class*="popup"], '
                    + '[class*="zm-modal"], [class*="consent"], [class*="recording-notice"], '
                    + '[class*="notify"], [class*="notice"]'
                );
                for (const el of containers) {
                    if (el.offsetParent === null) continue;
                    for (const btn of clickables(el)) {
                        const txt = (btn.textContent || btn.getAttribute('aria-label') || '')
                                    .trim().toLowerCase();
                        if (OK_LABELS.includes(txt) && btn.offsetParent !== null) {
                            const text = el.innerText.trim();
                            btn.click();
                            return text;
                        }
                    }
                }

                // Pass 2: <button> elements only — must be exactly "ok" to avoid over-matching
                for (const btn of document.querySelectorAll('button')) {
                    const txt = (btn.textContent || btn.getAttribute('aria-label') || '')
                                .trim().toLowerCase();
                    if (txt === 'ok' && btn.offsetParent !== null) {
                        const container = btn.closest('[class]');
                        const text = (container?.innerText || btn.parentElement?.innerText || '').trim();
                        if (!text) continue;
                        btn.click();
                        return text;
                    }
                }

                return null;
            })()
        """
        try:
            self._dialog_pending = True
            dialog_text = await page.evaluate(JS)
            if dialog_text:
                logger.info("Dismissed blocking dialog: %s", dialog_text[:80])
                if self.on_dialog:
                    try:
                        await self.on_dialog(dialog_text)
                    except Exception:
                        logger.exception("on_dialog callback failed")
        except Exception as e:
            logger.warning("_dismiss_blocking_dialogs failed: %s", e)
        finally:
            self._dialog_pending = False

    # ── Silence-monitor control (called by bot.py callbacks) ─────────────────

    def silence_snooze(self) -> None:
        """Suppress silence warnings for SILENT_SNOOZE_SECS seconds."""
        self._silence_state = "snoozed"
        self._snooze_until = time.monotonic() + SILENT_SNOOZE_SECS
        logger.info("Silence snoozed for %ds", SILENT_SNOOZE_SECS)

    def silence_wait_for_audio(self) -> None:
        """Enter wait-for-audio mode: no more silence warnings until audio returns."""
        self._silence_state = "waiting"
        logger.info("Silence: entering wait-for-audio mode")

    # ── Silence monitor ───────────────────────────────────────────────────────

    async def _silence_monitor(self) -> None:
        """Background task: watch for continuous silence while FFmpeg is recording.

        State machine:
          monitoring → warned     : silence ≥ SILENT_WARN_SECS   → on_silence_warn()
          snoozed    → warned     : snooze expired, still silent  → on_silence_warn()
          warned / waiting / snoozed → monitoring : audio returns → on_audio_returned()
              (on_audio_returned only fires when state was warned or waiting)

        External transitions (via bot.py callbacks):
          silence_snooze()         : warned → snoozed
          silence_wait_for_audio() : warned → waiting
        """
        if SILENT_WARN_SECS <= 0:
            return

        try:
            proc = await asyncio.create_subprocess_exec(
                "parec",
                f"--device={self.sink}",
                "--format=s16le",
                f"--rate={_AUDIO_SAMPLE_RATE}",
                "--channels=1",
                "--latency-msec=100",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("parec not found — silence monitor disabled")
            return

        chunk_bytes = int(_AUDIO_SAMPLE_RATE * 2 * _AUDIO_CHUNK_SECS)
        silent_since: float | None = None

        logger.info(
            "Silence monitor started (warn_secs=%d, rms_threshold=%d)",
            SILENT_WARN_SECS, AUDIO_RMS_THRESHOLD,
        )

        try:
            while not self._stop_event.is_set():
                if proc.returncode is not None:
                    logger.warning("Silence monitor: parec exited (code %d)", proc.returncode)
                    return

                try:
                    data = await asyncio.wait_for(proc.stdout.read(chunk_bytes), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if not data:
                    logger.warning("Silence monitor: parec EOF")
                    return

                n = len(data) // 2
                if n == 0:
                    continue

                samples = struct.unpack(f"{n}h", data[: n * 2])
                rms = (sum(s * s for s in samples) / n) ** 0.5
                now = time.monotonic()

                if rms <= AUDIO_RMS_THRESHOLD:
                    # ── SILENCE ───────────────────────────────────────────────
                    if silent_since is None:
                        silent_since = now
                    silence_secs = int(now - silent_since)

                    if self._silence_state == "monitoring" and silence_secs >= SILENT_WARN_SECS:
                        self._silence_state = "warned"
                        logger.info("Silence warning: %ds of continuous silence", silence_secs)
                        if self.on_silence_warn:
                            await self.on_silence_warn(silence_secs)

                    elif (
                        self._silence_state == "snoozed"
                        and now >= self._snooze_until
                        and silence_secs >= SILENT_WARN_SECS
                    ):
                        self._silence_state = "warned"
                        logger.info(
                            "Silence snooze expired — re-warning (%ds of silence)", silence_secs
                        )
                        if self.on_silence_warn:
                            await self.on_silence_warn(silence_secs)

                else:
                    # ── AUDIO ─────────────────────────────────────────────────
                    if silent_since is not None:
                        prev_state = self._silence_state
                        silence_secs = int(now - silent_since)
                        silent_since = None
                        self._silence_state = "monitoring"
                        self._snooze_until = 0.0

                        if prev_state in ("warned", "waiting"):
                            logger.info(
                                "Audio returned after %ds of silence (was '%s')",
                                silence_secs, prev_state,
                            )
                            if self.on_audio_returned:
                                await self.on_audio_returned()

        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, Exception):
                pass
            logger.info("Silence monitor stopped")

    async def _wait_for_audio(self) -> None:
        """Hold until audio is detected on the virtual sink, then return.

        Uses *parec* (PulseAudio record) to sample the virtual monitor source
        in 250 ms chunks.  Computes RMS of each chunk and returns as soon as
        the level exceeds AUDIO_RMS_THRESHOLD.

        Falls back to immediate return on:
          • AUDIO_WAIT_TIMEOUT == 0 (feature disabled)
          • parec binary not found
          • parec exits early (e.g. sink not ready)
          • _stop_event set (user called /stop before meeting started)
        """
        if AUDIO_WAIT_TIMEOUT <= 0:
            return

        chunk_bytes = int(_AUDIO_SAMPLE_RATE * 2 * _AUDIO_CHUNK_SECS)  # 1 ch, s16le

        try:
            proc = await asyncio.create_subprocess_exec(
                "parec",
                f"--device={self.sink}",
                "--format=s16le",
                f"--rate={_AUDIO_SAMPLE_RATE}",
                "--channels=1",
                "--latency-msec=100",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("parec not found — starting FFmpeg immediately (no audio wait)")
            return

        # Only notify "waiting" if silence persists beyond this many seconds.
        # Active meetings typically have audio within 3–5 s of joining.
        _NOTIFY_GRACE_SECS = 5.0

        start = time.monotonic()
        notified_waiting = False
        logger.info(
            "Waiting for audio on %s (threshold=%d RMS, no timeout)",
            self.sink, AUDIO_RMS_THRESHOLD,
        )

        try:
            while not self._stop_event.is_set():
                # parec exited early (e.g. sink not ready yet)
                if proc.returncode is not None:
                    logger.warning(
                        "parec exited (code %d) — starting FFmpeg immediately", proc.returncode
                    )
                    return

                try:
                    data = await asyncio.wait_for(
                        proc.stdout.read(chunk_bytes), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    pass
                else:
                    if not data:
                        logger.warning("parec EOF — starting FFmpeg immediately")
                        return

                    n = len(data) // 2
                    if n > 0:
                        samples = struct.unpack(f"{n}h", data[: n * 2])
                        rms = (sum(s * s for s in samples) / n) ** 0.5

                        if rms > AUDIO_RMS_THRESHOLD:
                            elapsed = time.monotonic() - start
                            logger.info(
                                "Audio detected — RMS=%.1f > threshold=%d at %.1fs elapsed",
                                rms, AUDIO_RMS_THRESHOLD, elapsed,
                            )
                            if notified_waiting and self.on_waiting:
                                await self.on_waiting("▶️ Audio detected — starting recording now!")
                            return

                # Send "waiting" notification only after grace period expires
                if not notified_waiting and (time.monotonic() - start) >= _NOTIFY_GRACE_SECS:
                    notified_waiting = True
                    if self.on_waiting:
                        await self.on_waiting(
                            "⏸ Joined meeting — waiting for audio to start recording.\n"
                            "Use /stop to abort."
                        )

        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _join_google_meet(self, page: Page, url: str) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)
        await self._screenshot(page, "01_gmeet_initial")

        # Dismiss "Do you want people to see and hear you?" dialog
        for _ in range(8):
            try:
                if await page.locator('input[placeholder="Your name"]').is_visible():
                    break
            except Exception:
                pass
            try:
                btn = page.get_by_text("Continue without microphone and camera", exact=True)
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    logger.info("GMeet: dismissed camera/mic dialog")
                    await asyncio.sleep(2)
                    break
            except Exception:
                pass
            await asyncio.sleep(2)

        await self._screenshot(page, "02_gmeet_name")

        # Fill name
        name_filled = False
        for sel in ['input[placeholder="Your name"]', 'input[type="text"]']:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.clear()
                    await el.fill(self.guest_name)
                    name_filled = True
                    logger.info("GMeet: name filled via %s", sel)
                    break
            except Exception:
                continue
        if not name_filled:
            logger.warning("GMeet: name field not found")

        # Click "Ask to join" or "Join now"
        join_clicked = False
        for label in ("Ask to join", "Join now"):
            try:
                btn = page.get_by_text(label, exact=True)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(timeout=5_000)
                    join_clicked = True
                    logger.info("GMeet: clicked '%s'", label)
                    break
            except Exception:
                continue
        if not join_clicked:
            logger.warning("GMeet: join button not found")

        await asyncio.sleep(3)
        await self._screenshot(page, "03_gmeet_joining")

        page_text = (await page.evaluate("document.body.innerText")).lower()
        if "you can't join this video call" in page_text or "cannot join this video call" in page_text:
            raise RuntimeError(
                "Google Meet: unable to join this video call. "
                "The meeting may require a signed-in Google account."
            )
        if "meet.google.com" not in page.url:
            raise RuntimeError(
                f"Google Meet: join redirected to unexpected page ({page.url!r}). "
                "Meeting may be invalid, not started, or require a signed-in Google account."
            )

    async def _watch_google_meet(self, page: Page) -> None:
        ended_phrases = [
            "you left the meeting",
            "this call has ended",
            "host ended the meeting",
            "you've been removed",
            "you were removed",
            "meeting has ended",
            "call ended",
        ]
        while not self._stop_event.is_set():
            try:
                if self._ffmpeg and self._ffmpeg.poll() is not None:
                    logger.error("FFmpeg exited unexpectedly (code %d)", self._ffmpeg.returncode)
                    if self.on_error:
                        await self.on_error(f"FFmpeg stopped unexpectedly (code {self._ffmpeg.returncode})")
                    return
                if page.is_closed():
                    return
                text = (await page.evaluate("document.body.innerText")).lower()
                for phrase in ended_phrases:
                    if phrase in text:
                        logger.info("GMeet: ended — '%s'", phrase)
                        return
                if "meet.google.com" not in page.url:
                    logger.info("GMeet: left URL: %s", page.url)
                    return
            except Exception as e:
                logger.debug("GMeet watch error: %s", e)
            await self._dismiss_blocking_dialogs(page)
            await asyncio.sleep(3)

    async def _join_meeting(self, page: Page, web_url: str) -> None:
        await page.goto(web_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)
        await self._screenshot(page, "01_initial")

        # Detect invalid / expired meeting before doing anything else
        page_text = (await page.evaluate("document.body.innerText")).lower()
        _ERROR_PHRASES = (
            "this meeting link is invalid",
            "meeting link is invalid",
            "invalid meeting",
            "meeting has ended",
            "this meeting is for authorized attendees only",
            "meeting is expired",
        )
        for phrase in _ERROR_PHRASES:
            if phrase in page_text:
                raise RuntimeError(f"Zoom error on join: {phrase}")

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

        # Fill passcode if provided — passcode and name fields appear on the same form,
        # so just fill the value; the existing name-fill + Join button click handles submission.
        if self.meeting_password:
            PASSCODE_SELECTORS = [
                'input[placeholder="Meeting Passcode"]',
                'input[placeholder="Passcode"]',
                'input[placeholder="passcode" i]',
                'input[type="password"]',
            ]
            for sel in PASSCODE_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        await el.fill(self.meeting_password)
                        logger.info("Passcode filled using: %s", sel)
                        break
                except Exception:
                    continue

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
            except Exception as e:
                logger.debug("Watch loop iteration error (will retry): %s", e)
            await self._dismiss_blocking_dialogs(page)
            await asyncio.sleep(3)

    _RESOLUTION_SCALE = {
        "360p": "640:360",
        "720p": "1280:720",
        "1080p": None,
    }

    def _build_vf(self, chrome_height: int) -> Optional[str]:
        """Build -vf filter string.

        Display is 1920×1200. Browser chrome sits at the top (chrome_height px).
        Crop away the chrome rows; output width stays 1920, height is whatever
        remains (rounded down to even for H.264 — typically ~1080–1200px).
        """
        crop_y = max(0, chrome_height)
        out_h = 1200 - crop_y
        out_h -= out_h % 2  # H.264 requires even dimensions
        if out_h <= 0:
            crop_y = 0
            out_h = 1200
        logger.info("Crop filter: 1920x%d at y=%d (chrome=%dpx)", out_h, crop_y, chrome_height)
        return f"crop=1920:{out_h}:0:{crop_y}" if crop_y > 0 else None

    def _start_ffmpeg(self, output: str, chrome_height: int = 0) -> subprocess.Popen:
        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "warning", "-nostats",
            "-f", "x11grab", "-r", "24", "-s", "1920x1200", "-i", self.display,
            "-f", "pulse", "-i", self.sink,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p",
        ]
        vf = self._build_vf(chrome_height)
        if vf:
            cmd += ["-vf", vf]
        cmd += [
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output,
        ]
        log_path = Path(output).parent / ".ffmpeg.log"
        self._ffmpeg_log = open(log_path, "w", encoding="utf-8")
        logger.info("FFmpeg log → %s", log_path)
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=self._ffmpeg_log,
            stderr=self._ffmpeg_log,
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
        if self._ffmpeg_log:
            try:
                self._ffmpeg_log.close()
            except Exception:
                pass
            self._ffmpeg_log = None
        logger.info("FFmpeg stopped")
