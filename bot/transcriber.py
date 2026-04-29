import asyncio
import os
import subprocess
from faster_whisper import WhisperModel

_model: WhisperModel | None = None


def _get_model(model_name: str) -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _model


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _transcribe_sync(mp4_path: str, model_name: str, language: str | None) -> tuple[str, str]:
    base = mp4_path[:-4]          # strip ".mp4"
    wav_path = base + ".wav"      # temp audio
    txt_path = base + ".txt"
    srt_path = base + ".srt"

    subprocess.run(
        [
            "ffmpeg", "-i", mp4_path,
            "-vn", "-ar", "16000", "-ac", "1",
            "-f", "wav", wav_path,
            "-y", "-loglevel", "error",
        ],
        check=True,
    )
    try:
        model = _get_model(model_name)
        segments, _ = model.transcribe(wav_path, language=language or None)
        segments = list(segments)  # consume generator before WAV is deleted

        txt_lines: list[str] = []
        srt_blocks: list[str] = []
        for i, seg in enumerate(segments, 1):
            text = seg.text.strip()
            txt_lines.append(text)
            srt_blocks.append(
                f"{i}\n"
                f"{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}\n"
                f"{text}\n"
            )

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines))
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(srt_blocks))

        return txt_path, srt_path
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


async def transcribe(
    mp4_path: str,
    model_name: str = "medium",
    language: str | None = "id",
) -> tuple[str, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, mp4_path, model_name, language)
