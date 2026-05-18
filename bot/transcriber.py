import asyncio
import logging
import os  # for WHISPER_THREADS env var
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# Default to 10 threads, leaving 2 cores free for the OS and bot overhead.
# Override with WHISPER_THREADS env var.
_CPU_THREADS = int(os.environ.get("WHISPER_THREADS", "10"))

_model: WhisperModel | None = None
_model_name: str | None = None


def _get_model(model_name: str) -> WhisperModel:
    global _model, _model_name
    if _model is None or _model_name != model_name:
        if _model is not None:
            logger.info("Switching Whisper model: %s → %s", _model_name, model_name)
        logger.info("Loading Whisper model '%s' with %d CPU threads", model_name, _CPU_THREADS)
        _model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=_CPU_THREADS,
        )
        _model_name = model_name
    return _model


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _transcribe_sync(mp4_path: str, model_name: str, language: str | None) -> tuple[str, str]:
    base = mp4_path[:-4]          # strip ".mp4"
    txt_path = base + ".txt"
    srt_path = base + ".srt"

    # faster-whisper decodes audio internally via ffmpeg — no temp WAV needed.
    # vad_filter skips silent segments before inference — speeds up long recordings
    # with dead time (waiting room, pauses, end-of-meeting silence).
    model = _get_model(model_name)
    segments, _ = model.transcribe(
        mp4_path,
        language=language or None,
        vad_filter=True,
    )

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


async def transcribe(
    mp4_path: str,
    model_name: str = "medium",
    language: str | None = "id",
) -> tuple[str, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, mp4_path, model_name, language)
