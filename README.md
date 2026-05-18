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
/record <zoom_url>
  → Playwright joins zoom.us/wc/<id>/join on a dedicated Xvfb display
  → FFmpeg x11grab + PulseAudio null sink → libx264 MP4
  → Bot watches DOM for meeting-end signal
  → On end/stop: saves MP4 to /recordings, notifies Telegram
```

## Bot commands

### BotFather `/setcommands`

```
record - Start recording a Zoom meeting
stop - Stop the active recording
peek - Screenshot with duration, size, and URL
schedule - View, reschedule, or cancel scheduled recordings
status - Active count + total across all users
history - Browse recordings, transcribe, rename, or send transcript
```

### Command reference

| Command | Description |
|---------|-------------|
| `/record [zoom_url]` | Start recording a Zoom meeting |
| `/stop` | Stop the active recording |
| `/peek` | Screenshot of the active meeting with duration, size, and URL |
| `/schedule` | View, reschedule, or cancel scheduled recordings |
| `/status` | Your active count + total across all users |
| `/history` | Browse recordings; transcribe, rename, send transcript, or AI summary |

### `/record` flow

1. **URL** — pass inline (`/record <url>`) or send after the prompt (100s timeout → cancelled)
2. **Bot name** — change the display name, or use the default (100s timeout → auto-selects default)
3. **Recording name** — type a label used as filename prefix, or skip (100s timeout → auto-name)
4. **Start now or schedule** — start immediately, or pick a future date/time in WIB (UTC+7)
5. Bot joins meeting muted + camera off → recording starts at 1080p

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
| Claude API (Anthropic) | AI meeting summary with optional web search |
| Filebrowser | Optional web UI to browse/download recordings |

## Project structure

```
zoomy/
├── bot/
│   ├── bot.py            # Telegram bot, command handlers, session flow
│   ├── recorder.py       # ZoomRecorder + DisplayPool (Playwright + FFmpeg)
│   ├── transcriber.py    # faster-whisper wrapper (MP4 → .txt + .srt)
│   ├── summarizer.py     # Claude API wrapper (transcript → AI summary)
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
GUEST_NAME=Zoomy                          # default bot display name in meetings
RECORDINGS_DIR=/recordings
DEBUG=false                               # set true to save debug screenshots

WHISPER_MODEL=medium                      # tiny | base | small | medium | large-v3
WHISPER_LANGUAGE=id                       # BCP-47 code, or leave empty for auto-detect
WHISPER_THREADS=10                        # CPU threads (defaults to all cores)

ANTHROPIC_API_KEY=                        # Claude API key; leave empty to disable AI Summary
SUMMARY_MODEL=claude-sonnet-4-6
SUMMARY_WEB_SEARCH=true                   # set false to disable web search (saves ~$0.03/summary)
```

Get your Telegram user ID from [@userinfobot](https://t.me/userinfobot).

### 3. Run

```bash
docker compose up -d --build
```

### 4. Access recordings

Filebrowser is included at port `7010`. On first run, check the container logs for the auto-generated password:

```bash
docker logs zoomy-files 2>&1 | grep -i password
```

You can also access recordings directly from the `recordings/` bind mount.

## `/history` actions

After selecting a recording from the `/history` listing:

| State | Available actions |
|---|---|
| No transcript | 🎙 Transcribe (Medium / Large) · ✏️ Rename |
| Transcript exists | 🔄 Retranscribe (Medium / Large) · 📄 Send Transcript · 🤖 AI Summary · ✏️ Rename |
| Transcript + Summary | 🔄 Retranscribe · 📄 Send Transcript · 🔄 Re-summarize · ✏️ Rename |

**Rename** renames the folder and all files inside that share the same stem prefix.

## AI Summary

Zoomy can generate a structured meeting summary from a transcript using Claude API.

**Flow:**
1. Click **🤖 AI Summary** or **🔄 Re-summarize** from `/history`, or from the button after transcription
2. Optionally provide meeting context (topic, participants, abbreviations) — or press **Skip**
3. Bot shows live status: `🔍 Mencari informasi di web…` / `✍️ Membuat ringkasan…`
4. Summary is sent, then a follow-up `[📄 Send Transcript]` button appears
5. Summary cached to `.summary.txt` inside the recording folder

**Summary structure:** Tentang Rapat · Pembahasan · Keputusan · Tindak Lanjut

**Web search:** Claude can search the web to look up unknown organizations or terms (`SUMMARY_WEB_SEARCH=true`). Costs ~$10/1000 searches on top of token fees. Set `SUMMARY_WEB_SEARCH=false` to disable.

**Built-in glossary:** BSSN, BSrE, Komdigi, PDP, KSS — no search needed for these.

## Scheduling

`/schedule` lists all pending recordings. Each entry has **[🔄 Reschedule]** and **[❌ Cancel]** buttons.

Reschedule flow: enter new date/time in natural language (WIB, UTC+7) → confirm → updated. 100s timeout per input step.

## Recording filename format

```
{name}_{YYYYMMDD}/          # named session   → folder + MP4 inside
{randomname}_{YYYYMMDD}/    # auto-named session
```

## Planned features

### Scheduling
- **Recurring schedule** — record the same meeting every day/week at the same time; ideal for standups
- **Pre-recording reminder** — send a Telegram message X minutes before a scheduled recording fires
- **Clone schedule** — duplicate a pending schedule to a new time without re-entering all details

### History & file management
- **Delete from `/history`** — delete a recording folder directly from the bot with a confirm step
- **Search in `/history`** — filter the listing by keyword instead of paginating through everything
- **Auto-cleanup** — automatically delete recordings older than a configurable number of days
- **Folder notes** — attach a short text note to a recording, stored in `.metadata.json` and shown in the listing
- **`/download`** — send the MP4 directly in chat for recordings under Telegram's 50 MB file limit

### URL handling
- **URL validation** — before joining, check if the Zoom meeting URL is valid, already ended, or requires a password; give a clear error instead of silently failing
- **Shortened URL resolution** ⭐ — automatically follow redirects from bit.ly, tinyurl, s.id, etc. and extract the real Zoom URL before joining

### Recording control
- **Pause/resume** — pause FFmpeg mid-recording (`SIGSTOP`/`SIGCONT`) when a meeting goes off-topic, resume when it gets back on track
- **Audio-only mode** — record audio track only; much smaller files and faster transcription for voice-heavy meetings
- **Quality presets** — choose low/medium/high at `/record` time (720p smaller file vs current 1080p)
- **Recording markers** — send `/mark <label>` during an active recording to drop a named timestamp into `.metadata.json`; useful for jumping to key moments later
- **Stop with note** — `/stop <note>` appends a short note to metadata when stopping
- **Trim on stop** — optionally cut the first/last N seconds off the saved MP4 to remove joining dead time and end-of-meeting goodbyes
- **Clip extraction** — extract a specific time range (e.g. `10:30–15:00`) from a recording as a shorter clip via FFmpeg; no re-encode needed

### Recording reliability
- **Rejoin on kick** — if Zoom removes the bot (waiting room, meeting lock), auto-rejoin once and notify
- **Silent audio warning** ⭐ — detect if FFmpeg captures silence for >60 s and warn the user (catches broken PulseAudio sinks)
- **Disk space alert** — notify when free space on `/recordings` drops below a configurable threshold

### During the meeting
- **Zoom chat capture** — scrape the in-meeting chat from the browser DOM and save it alongside the recording; participants lose the chat log when the meeting ends
- **Attendance log** — periodically scrape the participant list from the DOM and save to `.metadata.json`

### Transcription & analysis
- **Multilingual / bilingual transcription** ⭐ — pass `language=None` to let Whisper auto-detect per segment; useful for meetings that mix Indonesian and English in the same recording
- **Speaker diarization** — label transcript segments with who spoke ("Speaker 1:", "Speaker 2:") using `pyannote.audio`; the single most useful feature for long multi-person meetings
- **Action item extraction** — scan the transcript for patterns like "saya akan…", "kita perlu…", "tolong…" and list them out; no LLM required, keyword/regex based
- **Subtitle burn-in** — embed the SRT file directly into the MP4 with FFmpeg; one button in `/history`, no extra storage needed
- **Meeting minutes template** — restructure the transcript into a formatted doc: date, duration, participants, key points, action items
- **Transcript stats** — show word count and estimated speaking time after transcription completes
- ~~**Transcript summary**~~ ✅ — AI summary via Claude API (Sonnet 4.6); optional web search for unknown terms; cached to `.summary.txt`; user provides meeting context before generation
- **Transcript translation** — translate the transcript to another language after transcription
- **Chapter export** — if recording markers were dropped during a session, auto-generate an FFmpeg chapter metadata file so video players show named chapters

### Search & discovery
- **`/search <keyword>`** — search across all transcript `.txt` files and return which recordings mention the keyword; no new dependencies, just file grep
- **Talk time per speaker** — if diarized, show percentage of time each speaker talked
- **Meeting length trend** — `/stats` showing average meeting duration over the past month

### Bot UX
- **`/help`** — inline command reference with usage examples
- **Webhook on stop** — POST recording metadata to a configurable URL when a recording finishes; enables external automation (auto-upload, pipeline triggers, etc.)
- **Telegram channel push** — optionally forward the stop summary to a group or channel after recording
- **Multi-language bot UI** — bot responses in Indonesian (`LANG=id`) since that's the primary use language
- **Pin active recording message** — pin the "recording started" message in chat so it's always visible and easy to `/stop` from

### Notifications
- **Recording size milestone** — notify when a single recording crosses 1 GB, 2 GB, etc. before it fills the disk
- **Daily digest** — every morning, send a summary of what was recorded the previous day (name, duration, size) to each authorized user

### Multi-user & admin
- **User management from bot** — `/admin adduser <id>` and `/admin removeuser <id>` without touching `.env` and restarting the container
- **Per-user storage quota** — limit how much disk space each authorized user's recordings can consume
- **`/stats`** — total recordings, total hours recorded, total disk used since the bot started

### Observability
- **Error report** — if Playwright or FFmpeg crashes mid-recording, send the last 20 lines of the FFmpeg log to the user automatically
- **Recording log** — persistent log of every recording ever made (start time, duration, size, stop reason), including deleted ones

### Zoom-specific
- **Password support** ⭐ — accept Zoom links with embedded passwords or a separate password argument
- **Waiting room notification** ⭐ — detect when the bot is stuck in a waiting room and notify the user to admit it, with a configurable timeout before giving up
- **Participant count detection** — notify if the meeting drops to 1 person (everyone left but the host hasn't ended)
- **Screen share detection** — detect when someone starts sharing their screen and log the timestamp into metadata
- **Gallery vs speaker view** — let user pick Zoom layout before joining

### Integrations
- **Google Calendar** — poll a calendar and auto-schedule recordings when a Zoom link is detected in an event
- **Auto-export transcript** — after transcription, write the `.txt` to a mounted Obsidian vault or shared folder automatically
- **Pre-join screenshot** — take a screenshot of the Zoom waiting room before fully joining so the user can confirm it's the right meeting

## Recording crop

Each session runs on a **1920×1200 virtual display** (Xvfb). The extra 120px of vertical space gives the browser chrome (address bar, tab bar) room to exist without eating into the meeting content.

After joining the meeting, the bot measures the browser chrome height once via JavaScript:

```js
window.outerHeight - window.innerHeight  // e.g. 90px
```

FFmpeg then starts with a `-vf crop=1920:{h}:0:{chrome_height}` filter baked in — applied live, frame-by-frame from the first second. The saved MP4 already has the chrome stripped; no post-processing pass is needed.

Example: chrome = 90px → `crop=1920:1110:0:90` → output is 1920×1110  
Example: chrome = 120px → `crop=1920:1080:0:120` → output is 1920×1080

Output is always ~1080p tall (exact height depends on the measured chrome), width stays 1920px.

## Notes

- Requires Docker with `shm_size: 2gb` (set in `docker-compose.yml`) for Chromium stability
- Bot joins meetings muted with camera off
- Meeting-end detection polls `document.body.innerText` every 3s — stops automatically when the host ends the meeting
- Stop notification includes duration, file size, and stop reason (host ended vs manually stopped)
- 1080p captures the full Xvfb display unscaled; 720p/360p apply `-vf scale=`
- Filebrowser requires a pre-created empty file at `filebrowser/filebrowser.db` (Docker will create a directory otherwise):
  ```bash
  mkdir -p filebrowser && touch filebrowser/filebrowser.db
  ```
