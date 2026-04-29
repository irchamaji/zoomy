FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV RECORDINGS_DIR=/recordings
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Virtual display
    xvfb \
    x11-xserver-utils \
    # Virtual audio
    pulseaudio \
    pulseaudio-utils \
    # Recording
    ffmpeg \
    # Python
    python3 \
    python3-pip \
    # Playwright system deps
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libasound2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libexpat1 \
    libfontconfig1 \
    libglib2.0-0 \
    libnspr4 \
    # Misc
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

ARG WHISPER_MODEL=medium
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

COPY . .
RUN chmod +x entrypoint.sh

VOLUME ["/recordings"]

ENTRYPOINT ["./entrypoint.sh"]
