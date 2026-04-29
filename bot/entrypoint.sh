#!/bin/bash
set -e

# Minimal base display — PulseAudio needs an X context on Ubuntu
# Recording sessions use :99, :100, ... (managed by DisplayPool)
rm -f /tmp/.X0-lock
Xvfb :0 -screen 0 1x1x24 -ac -noreset &
export DISPLAY=:0
sleep 1

# PulseAudio daemon — sessions create their own null sinks via pactl
pulseaudio --start --exit-idle-time=-1 --log-level=error
sleep 1

echo "[zoomy] PulseAudio ready (display=:0). Starting bot..."

exec python3 bot.py
