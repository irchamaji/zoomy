# Zoomy

A Telegram bot that records Zoom meetings by joining as a browser guest — no Zoom account or host access required.

**Bot:** [@baboonrecord_bot](https://t.me/baboonrecord_bot)  
**Hosted on:** Baboon Homeserver (`192.168.68.110`)

## How it works

Zoomy uses Playwright to join a Zoom web client session on a headless Xvfb display, then captures the screen and audio with FFmpeg.

```
Telegram /record <url>
  → Playwright joins zoom.us/wc/<id>/join (headless=False on Xvfb :99)
  → FFmpeg x11grab + PulseAudio → libx264 MP4
  → Bot watches DOM for meeting-end signal
  → On end/stop: saves MP4 to /recordings, notifies Telegram
```

## Bot commands

| Command | Description |
|---------|-------------|
| `/record <zoom_url>` | Start recording a Zoom meeting |
| `/stop` | Stop the active recording |
| `/ongoing` | Show active recordings with duration, size, URL |
| `/status` | Quick active recording count |

### `/record` flow

1. **Bot name** — `[Change Bot Name]` or `[Use Zoomy]` (resets to default each session)
2. **Recording name** — type a label (used as filename prefix), or `[Skip]` / wait 100s for auto-name
3. **Resolution** — `[360p]` `[720p]` `[1080p]`
4. Bot joins meeting muted + camera off → recording starts

## Stack

| Component | Role |
|-----------|------|
| Playwright (Chromium) | Joins Zoom web client, watches for meeting end |
| Xvfb `:99` 1920×1080 | Virtual display for headless browser |
| PulseAudio null sink | Virtual audio device for FFmpeg capture |
| FFmpeg x11grab + pulse | Screen + audio → MP4 (libx264, yuv420p, faststart) |
| python-telegram-bot v21 | Telegram bot interface |
| Filebrowser (port 7010) | Web UI to browse/download recordings |

## Project structure

```
zoomy/
├── bot/
│   ├── bot.py            # Telegram bot, state machine, commands
│   ├── recorder.py       # ZoomRecorder: Playwright + FFmpeg
│   ├── entrypoint.sh     # Starts Xvfb, PulseAudio, then bot.py
│   ├── Dockerfile
│   └── requirements.txt
├── filebrowser/
│   └── settings.json
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
AUTHORIZED_USER_IDS=123456789,987654321   # comma-separated Telegram user IDs
GUEST_NAME=Zoomy                          # default bot display name in meetings
RECORDINGS_DIR=/recordings
DEBUG=false                               # set true to save debug screenshots
```

### 3. Deploy

```bash
# On Baboon
cd ~/docker/zoomy
docker compose up -d --build
```

### 4. Access recordings

Filebrowser runs at `http://192.168.68.110:7010` — default creds shown in container logs on first run.

## Deploy from Mac

```bash
scp bot/bot.py bot/recorder.py ircham@192.168.68.110:~/docker/zoomy/bot/
ssh ircham@192.168.68.110 "cd ~/docker/zoomy && docker compose up -d --build"
```

## Recording filename format

```
{prefix}_{YYYYMMDD}_{HHMMSS}_{meetingId}.mp4   # with prefix
{YYYYMMDD}_{HHMMSS}_{meetingId}.mp4            # auto-name
```

## Notes

- Bot joins meetings muted with camera off to avoid being visible
- Meeting-end detection polls `document.body.innerText` every 3s for Zoom end-of-meeting phrases (visible text only — avoids false-positives from JS bundle source)
- Stop notification includes duration, file size, and reason (host ended vs manually stopped)
- 1080p captures the full Xvfb display; 720p/360p downscale via `-vf scale=`
- Supports multiple concurrent recordings (one per chat)
- Filebrowser requires a bind-mounted empty file at `filebrowser/filebrowser.db` (not a directory)
