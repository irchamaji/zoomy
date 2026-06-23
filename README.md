# Zoomy

A Telegram bot that records Zoom meetings by joining as a browser guest — no Zoom account or host access required.

## Demo

**Bot flow** (`/record` → schedule → join → stop)

![Bot flow](demo-howto.gif)

**Recording output sample**

![Recording output sample](demo-output.png)

## How it works

Zoomy uses Playwright to join a Zoom web client session on a virtual display (Xvfb), then captures the screen and audio with FFmpeg.

```
/record <zoom_url|meeting_id>
  → URL validated + shortened URLs auto-resolved
  → Playwright joins zoom.us/wc/<id>/join on a dedicated Xvfb display
  → Bot waits for first audio on the virtual sink (parec RMS check)
  → FFmpeg x11grab + PulseAudio null sink → libx264 MP4
  → Bot watches DOM for meeting-end signal
  → On end/stop: saves MP4 to /recordings, notifies Telegram + screenshot
```

## Bot commands

### BotFather `/setcommands`

```
record - Start recording a Zoom meeting
stop - Stop the active recording
peek - Screenshot with duration, size, and URL
schedule - View, reschedule, or cancel scheduled recordings
status - Active count + total across all users
history - Browse recordings, transcribe, rename, or AI summary
```

### Command reference

| Command | Description |
|---------|-------------|
| `/record [url_or_id]` | Start recording a Zoom meeting |
| `/stop` | Stop the active recording |
| `/peek` | Screenshot of the active meeting with duration, size, and URL |
| `/schedule` | View, reschedule, or cancel scheduled recordings |
| `/status` | Your active count + total across all users |
| `/history` | Browse recordings; transcribe, rename, send transcript, or AI summary |

Any unknown command (including `/start`) returns the command list.

### `/record` flow

1. **URL or Meeting ID** — pass inline or send after the prompt (100s timeout → cancelled)
   - Full URL with password: `https://zoom.us/j/97856007427?pwd=xxx`
   - URL without password: `https://zoom.us/j/97856007427`
   - Meeting ID only: `97856007427` or `978 5600 7427`
   - Shortened URL: `bit.ly/meeting` — auto-resolved via HTTP redirect
2. **Password** — if URL/ID has no embedded password, bot asks for the plain passcode (Skip to proceed without)
3. **Bot name** — change the display name, or use the default (100s timeout → auto-selects default)
4. **Recording name** — type a label used as filename prefix, or skip (100s timeout → auto-name)
5. **Start now or schedule** — start immediately, or pick a future date/time in WIB (UTC+7)
6. Bot joins meeting muted + camera off
7. **Audio wait** — if `AUDIO_WAIT_TIMEOUT` is non-zero, FFmpeg is held until audio is detected on the virtual sink. If silence persists beyond 5 s, a "⏸ waiting…" notification fires; when audio is detected, a "▶️ started" notification follows. In an active meeting audio is usually detected immediately with no notification at all. Use `/stop` to abort while waiting.
8. Recording starts → initial screenshot sent

Multiple concurrent recordings are supported. Each session gets its own isolated display and audio sink. Per-user isolation: each authorized user manages only their own sessions.

## Stack

| Component | Role |
|-----------|------|
| Playwright (Chromium) | Joins Zoom web client, watches for meeting end |
| Xvfb (per session) | Virtual display — one per active recording |
| PulseAudio null sink (per session) | Virtual audio device for FFmpeg capture |
| FFmpeg x11grab + pulse | Screen + audio → MP4 (libx264, yuv420p, faststart) |
| python-telegram-bot v21 | Telegram bot interface |
| faster-whisper | Local speech-to-text transcription (CPU, int8) |
| Gemini API (Google) | AI meeting summary with optional Google Search grounding |

## Project structure

```
zoomy/
├── bot/
│   ├── bot.py            # Telegram bot, command handlers, session flow
│   ├── recorder.py       # ZoomRecorder + DisplayPool (Playwright + FFmpeg)
│   ├── transcriber.py    # faster-whisper wrapper (MP4 → .txt + .srt)
│   ├── summarizer.py     # Gemini API wrapper (transcript → AI summary)
│   ├── store.py          # SessionStore — per-user in-memory state
│   ├── entrypoint.sh     # Container startup (PulseAudio + Xvfb base)
│   ├── Dockerfile
│   └── requirements.txt
├── recordings/           # MP4 output (bind-mounted into container)
├── docker-compose.yml
└── .env.example
```

## Setup

### 1. Clone and configure

```bash
git clone <repo>
cd zoomy
cp .env.example .env
# Edit .env with your values
```

### 2. Environment variables (`.env`)

```env
TELEGRAM_TOKEN=your_bot_token
AUTHORIZED_USER_IDS=111111111,222222222   # comma-separated Telegram user IDs; leave empty to allow all
GUEST_NAME=zoomy.ircham.dev               # default bot display name in meetings
RECORDINGS_DIR=/recordings
DEBUG=false                               # set true to save debug screenshots

WHISPER_MODEL=medium                      # tiny | base | small | medium | large-v3
WHISPER_LANGUAGE=id                       # BCP-47 code, or leave empty for auto-detect
WHISPER_THREADS=                          # CPU threads (defaults to all cores)

GEMINI_API_KEY=                           # Gemini API key; leave empty to disable AI Summary
SUMMARY_MODEL=gemini-3.5-flash            # model used when web search is OFF
SUMMARY_WEB_SEARCH_MODEL=gemini-2.5-flash # model used when web search is ON
SUMMARY_PREPROCESS_MODEL=gemini-3.1-flash-lite  # cheap model for unknown-term extraction
SUMMARY_WEB_SEARCH=true                   # set false to disable Google Search grounding

AUDIO_WAIT_TIMEOUT=1     # non-zero = wait for audio before starting FFmpeg; 0 = start immediately
AUDIO_RMS_THRESHOLD=100  # RMS level (0–32767) that counts as "audio present"

WHISPER_INITIAL_PROMPT=  # domain context injected into Whisper's context window (leave empty for built-in default)
WHISPER_HOTWORDS=        # comma-separated words to boost during beam search (leave empty for built-in default)
```

Get your Telegram user ID from [@userinfobot](https://t.me/userinfobot).  
Get a Gemini API key at [aistudio.google.com](https://aistudio.google.com) — free tier (attach a billing account to activate quotas, no charge required).

### 3. Run

```bash
docker compose up -d --build
```

### 4. Access recordings

Recordings are available via the `recordings/` bind mount, or through FileBrowser Quantum at port `7011`.

## `/history` actions

After selecting a recording from the `/history` listing:

| State | Available actions |
|---|---|
| No transcript | 🎙 Transcribe (Medium / Large) · ✏️ Rename |
| Transcript exists | 🔄 ReTC (Medium / Large) · 📄 Send Transcript · 🤖 AI Summary · ✏️ Rename |
| Transcript + Summary | 🔄 ReTC · 📄 Send Transcript · 📋 Send Summary · 🔄 Re-summarize · ✏️ Rename |

**Rename** renames the folder and all files inside that share the same stem prefix.

## AI Summary

Zoomy generates structured meeting summaries using the Gemini API.

**Flow:**
1. Click **🤖 AI Summary** or **🔄 Re-summarize** from `/history`, or from the button after transcription
2. Bot pre-processes transcript with a cheap model (`gemini-3.1-flash-lite`) to identify missing context or ambiguous references
3. If gaps found, bot shows clarification questions — answer or press **Skip**
4. Bot shows live status: `🔍 Mencari informasi di web…` / `✍️ Membuat ringkasan…`
5. Summary streams live to Telegram
6. Summary cached to `.summary.txt` inside the recording folder
7. Follow-up `[📄 Send Transcript]` button appears

**Summary structure:**
```
💭 [one-sentence context understanding]

**Tentang Rapat** · **Pembahasan** · **Keputusan** · **Tindak Lanjut**
```

**Web search:** Gemini can use Google Search grounding to look up unknown organizations or terms (`SUMMARY_WEB_SEARCH=true`). Included in the Gemini free tier.

**Built-in glossary:** BSSN, BSrE, Komdigi, Kominfo, PDP, KSS, D32, TTE, SPBE, Pemdi/Pemdigi, PSrE, NSPK, RKA

**Thinking mode:** Gemini uses internal reasoning (`thinking_budget=8192`) before generating — improves quality without adding to output length.

## Transcription

Zoomy uses **faster-whisper** for local, CPU-only transcription (no cloud API needed).

| Setting | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `medium` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `WHISPER_LANGUAGE` | `id` | BCP-47 code; leave empty for auto-detect |
| `WHISPER_THREADS` | all cores | CPU thread count |

**VAD filter** — silent segments (waiting room, pauses) are skipped before inference, keeping transcription fast.

**Domain vocabulary** is injected in two ways:

- `WHISPER_INITIAL_PROMPT` — primes Whisper's context window with a meeting description, teaching spelling and style of domain terms (BSSN, BSrE, TTE, SPBE, Pemdigi, etc.)
- `WHISPER_HOTWORDS` — boosts token probability during beam search for a comma-separated list of terms

Both have sensible defaults covering Indonesian government / cybersecurity vocabulary (BSSN, Komdigi, IKASANDI, IKAMI, PDP, KSS, D32, BSrE, PSrE, NSPK, RKA, SPBE, TTE, Pemdigi, Pemdi, IPPD, LPPD, TTIS, CSIRT, Sanapati, Forkomsanda, Manrisk, PTKKSS, and more). Override via env vars.

## Scheduling

`/schedule` lists all pending recordings. Each entry has **[🔄 Reschedule]** and **[❌ Cancel]** buttons.

Reschedule flow: enter new date/time in natural language (WIB, UTC+7) → confirm → updated. 100s timeout per input step.

Schedules persist across bot restarts via `recordings/schedules.json`. On startup, missed schedules trigger a user notification; future schedules are re-queued with the remaining delay.

## Recording filename format

```
{name}_{YYYYMMDD}/          # named session   → folder + MP4 inside
{randomname}_{YYYYMMDD}/    # auto-named session
```

## Planned features

### Platform support
- **Google Meet** — join and record `meet.google.com/xxx-xxxx-xxx` links; same workflow as Zoom. Currently blocked on meetings restricted to signed-in Google accounts; needs a dedicated bot Google account with stored session cookies to support those.

### Scheduling
- **Recurring schedule** — record the same meeting every day/week at the same time; ideal for standups
- **Pre-recording reminder** — send a Telegram message X minutes before a scheduled recording fires
- **Clone schedule** — duplicate a pending schedule to a new time without re-entering all details

### History & file management
- **Delete from `/history`** — delete a recording folder directly from the bot with a confirm step
- **Search in `/history`** — filter the listing by keyword instead of paginating through everything
- **Auto-cleanup** — automatically delete recordings older than a configurable number of days
- **`/download`** — send the MP4 directly in chat for recordings under Telegram's 50 MB file limit

### Recording control
- **Pause/resume** — pause FFmpeg mid-recording (`SIGSTOP`/`SIGCONT`) when a meeting goes off-topic
- **Audio-only mode** — record audio track only; much smaller files and faster transcription
- **Recording markers** — send `/mark <label>` during an active recording to drop a named timestamp
- **Trim on stop** — optionally cut the first/last N seconds off the saved MP4
- **Clip extraction** — extract a time range from a recording as a shorter clip via FFmpeg

### Recording reliability
- **Rejoin on kick** — if Zoom removes the bot (waiting room, meeting lock), auto-rejoin once and notify
- ~~**Silent audio warning**~~ ✅ — detect continuous silence mid-recording; Telegram warning after `SILENT_WARN_SECS` with three actions: **💤 Remind me in 5m** (suppress warning for `SILENT_SNOOZE_SECS`, still recording), **🔔 Alert when audio returns** (notify when audio comes back, still recording), **⏹ Stop recording**; re-warns after snooze expires if still silent
- **Disk space alert** — notify when free space on `/recordings` drops below a configurable threshold
- ~~**Audio-wait on join**~~ ✅ — hold FFmpeg until first audio is detected on the virtual sink after joining; Telegram notified on wait start ("⏸") and on audio detection ("▶️"); `/stop` works while waiting; configurable via `AUDIO_WAIT_TIMEOUT` / `AUDIO_RMS_THRESHOLD`

### During the meeting
- **Zoom chat capture** — scrape the in-meeting chat from the browser DOM and save alongside the recording
- **Attendance log** — periodically scrape the participant list from the DOM and save to `.metadata.json`
- **Waiting room notification** ⭐ — detect when the bot is stuck in a waiting room and notify the user

### Transcription & analysis
- **Multilingual / bilingual transcription** — auto-detect language via `language=None` in Whisper; useful for meetings that mix Indonesian and English
- **Speaker diarization** — label transcript segments with who spoke using `pyannote.audio`
- **Action item extraction** — scan transcript for "saya akan…", "kita perlu…", etc.
- **Subtitle burn-in** — embed the SRT file directly into the MP4 with FFmpeg
- ~~**Transcript summary**~~ ✅ — AI summary via Gemini API; Google Search grounding; pre-processor detects context gaps; cached to `.summary.txt`
- **Transcript translation** — translate the transcript to another language after transcription

### Search & discovery
- **`/search <keyword>`** — search across all transcript `.txt` files and return matching recordings
- **Talk time per speaker** — if diarized, show percentage of time each speaker talked

### Bot UX
- **Webhook on stop** — POST recording metadata to a configurable URL when a recording finishes
- **Telegram channel push** — optionally forward the stop summary to a group or channel
- **Pin active recording message** — pin the "recording started" message in chat

### Multi-user & admin
- **User management from bot** — `/admin adduser <id>` and `/admin removeuser <id>`
- **Per-user storage quota** — limit disk space per authorized user
- **`/stats`** — total recordings, total hours, total disk used

### Integrations
- **Google Calendar** — poll a calendar and auto-schedule when a Zoom link is detected in an event
- **Auto-export transcript** — after transcription, write `.txt` to a mounted Obsidian vault or shared folder

## Recording crop

Each session runs on a **1920×1200 virtual display** (Xvfb). The extra 120px gives the browser chrome room to exist without eating into meeting content.

After joining, the bot measures browser chrome height once via JavaScript:

```js
window.outerHeight - window.innerHeight  // e.g. 90px
```

FFmpeg starts with a `-vf crop=1920:{h}:0:{chrome_height}` filter baked in — applied live from the first frame. The saved MP4 has chrome stripped; no post-processing needed.

Example: chrome = 90px → `crop=1920:1110:0:90` → output is 1920×1110  
Example: chrome = 120px → `crop=1920:1080:0:120` → output is 1920×1080

## Notes

- Requires Docker with `shm_size: 2gb` (set in `docker-compose.yml`) for Chromium stability
- Bot joins meetings muted with camera off
- Meeting-end detection polls `document.body.innerText` every 3s — stops automatically when the host ends the meeting
- Stop notification includes duration, file size, and stop reason (host ended vs manually stopped)
- FFmpeg encodes at CRF 26, `veryfast` preset, 24fps — balanced quality/size for meeting content
- Invalid meeting links (error 3001, expired, etc.) are detected on join and reported immediately
