# Zoomy

A Telegram bot that records Zoom meetings by joining as a browser guest — no Zoom account or host access required.

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

| Command | Description |
|---------|-------------|
| `/record <zoom_url>` | Start recording a Zoom meeting |
| `/stop` | Stop the active recording |
| `/ongoing` | Show active recordings with duration, size, URL |
| `/status` | Your active count + total across all users |

### `/record` flow

1. **Bot name** — change the display name for this session, or use the default
2. **Recording name** — type a label (used as filename prefix), or skip for auto-name
3. **Resolution** — `[360p]` `[720p]` `[1080p]`
4. Bot joins meeting muted + camera off → recording starts

Multiple concurrent recordings are supported. Each session gets its own isolated display and audio sink. Per-user isolation: each authorized user manages only their own sessions.

## Stack

| Component | Role |
|-----------|------|
| Playwright (Chromium) | Joins Zoom web client, watches for meeting end |
| Xvfb (per session) | Virtual display — one per active recording |
| PulseAudio null sink (per session) | Virtual audio device for FFmpeg capture |
| FFmpeg x11grab + pulse | Screen + audio → MP4 (libx264, yuv420p, faststart) |
| python-telegram-bot v21 | Telegram bot interface |
| Filebrowser | Optional web UI to browse/download recordings |

## Project structure

```
zoomy/
├── bot/
│   ├── bot.py            # Telegram bot, command handlers, session flow
│   ├── recorder.py       # ZoomRecorder + DisplayPool (Playwright + FFmpeg)
│   ├── store.py          # SessionStore — per-user in-memory state
│   ├── entrypoint.sh     # Container startup (PulseAudio + Xvfb base)
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
AUTHORIZED_USER_IDS=111111111,222222222   # comma-separated Telegram user IDs; leave empty to allow all
GUEST_NAME=Zoomy                          # default bot display name in meetings
RECORDINGS_DIR=/recordings
DEBUG=false                               # set true to save debug screenshots
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

## Recording filename format

```
{name}_{YYYYMMDD}_{HHMMSS}_{meetingId}.mp4   # named session
{YYYYMMDD}_{HHMMSS}_{meetingId}.mp4            # auto-named session
```

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
