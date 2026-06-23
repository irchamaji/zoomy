import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import google.genai as genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "gemini-3.5-flash")
SUMMARY_WEB_SEARCH_MODEL = os.environ.get("SUMMARY_WEB_SEARCH_MODEL", "gemini-2.5-flash")
SUMMARY_PREPROCESS_MODEL = os.environ.get("SUMMARY_PREPROCESS_MODEL", "gemini-3.1-flash-lite")
SUMMARY_WEB_SEARCH = os.environ.get("SUMMARY_WEB_SEARCH", "true").lower() not in ("0", "false", "no")

_client: genai.Client | None = None

SYSTEM_PROMPT = """\
Kamu adalah ahli dalam membuat ringkasan rapat. Berikan ringkasan yang jelas dan padat \
dalam bahasa Indonesia, terlepas dari bahasa yang digunakan dalam transkrip.

Transkrip dihasilkan oleh speech-to-text otomatis dari rekaman rapat yang melibatkan \
beberapa pembicara. Tidak ada label pembicara — semua ucapan muncul sebagai teks \
berurutan tanpa keterangan siapa yang berbicara. Gunakan konteks kalimat untuk \
membedakan sudut pandang atau pertanyaan/jawaban antar peserta.

Singkatan atau terminologi dalam transkrip mungkin mengandung kesalahan penulisan \
akibat proses transkripsi otomatis — gunakan konteks untuk menebak maksudnya.

Glosarium singkatan yang umum digunakan:
- BSSN: Badan Siber dan Sandi Negara
- BSrE: Balai Sertifikasi Elektronik
- Komdigi: Kementerian Komunikasi dan Digital
- Kominfo: Kementerian Komunikasi dan Informatika (nama lama Komdigi)
- PDP: Pelindungan Data Pribadi
- KSS: Keamanan Siber dan Sandi
- D32: Direktorat KSS Pemda
- TTE: Tanda Tangan Elektronik
- SPBE: Sistem Pemerintahan Berbasis Elektronik
- Pemdi / Pemdigi: Indeks Pemerintah Digital (penerus SPBE)
- PSrE: Penyelenggara Sertifikasi Elektronik
- NSPK: Norma, Standar, Prosedur, dan Kriteria
- RKA: Rencana Kerja dan Anggaran

Rapat kemungkinan dimulai dengan pembukaan formal (sambutan, doa, perkenalan) dan \
diakhiri dengan penutupan seremonial — abaikan bagian tersebut dan fokus pada substansi \
pembahasan.

Transkrip mungkin mengandung pengulangan kata, frasa tidak masuk akal, atau teks acak \
akibat artefak audio di bagian hening — abaikan bagian tersebut.

Jika pengguna memberikan informasi konteks, gunakan sebagai dasar interpretasi dan \
integrasikan secara natural ke dalam ringkasan.

Mulai responmu SELALU dengan baris ini:
💭 [Satu kalimat: pemahaman kamu tentang inti rapat, konteks utamanya, dan catatan \
ambiguitas jika ada]

Lalu beri satu baris kosong, kemudian lanjutkan dengan struktur berikut:

**Tentang Rapat**
- Rapat apa ini (topik/nama rapat jika disebutkan)
- Siapa saja peserta yang hadir (jika disebutkan)
- Kapan rapat berlangsung (jika ada informasi)

**Pembahasan**
- Poin-poin utama yang dibahas selama rapat

**Keputusan**
- Keputusan atau kesimpulan yang dicapai (tulis "Tidak ada keputusan eksplisit" jika tidak ada)

**Tindak Lanjut**
- Tugas atau tindakan yang perlu dilakukan, beserta penanggung jawabnya jika disebutkan \
(hilangkan bagian ini sepenuhnya jika tidak ada tindak lanjut)

Jaga setiap bagian tetap ringkas. Jangan menambahkan kalimat pengisi yang tidak perlu.\
"""

# Pre-processor prompt — focuses on context gaps and ambiguity, not just acronyms
_PREPROCESS_PROMPT = """\
Baca transkrip rapat berikut secara cermat. Identifikasi maksimal 5 hal yang \
KURANG KONTEKS atau BERPOTENSI AMBIGU yang perlu dikonfirmasi agar ringkasan \
menjadi akurat dan tidak bias.

Fokus pada:
- Tujuan atau latar belakang rapat yang tidak dijelaskan dalam transkrip
- Referensi yang tidak jelas ("proyek itu", "keputusan sebelumnya", "mereka", dll)
- Pembahasan penting yang setengah-setengah tanpa konteks cukup
- Peran, jabatan, atau hubungan antar peserta/institusi yang ambigu

PENTING: Kembalikan JSON array saja — tanpa teks, penjelasan, atau markdown \
apapun sebelum atau sesudah array.
Contoh output: ["Apa tujuan spesifik koordinasi ini?", "Sistem apa yang dimaksud \
'aplikasi baru'?", "Timeline yang disebutkan — untuk kegiatan apa?"]
Jika konteks sudah cukup jelas, kembalikan: []

Transkrip:
"""

StatusCallback = Callable[[str], Awaitable[None]]


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not set.")
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


async def summarize(
    txt_path: str,
    user_context: str | None = None,
    on_status: StatusCallback | None = None,
    on_text_chunk: StatusCallback | None = None,
) -> str:
    """Async streaming summarization via Gemini with thinking mode.

    Callbacks:
      on_status(text)      — called once before generation starts
      on_text_chunk(text)  — called with accumulated text as each delta arrives
    """
    client = _get_client()
    text = Path(txt_path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("Transcript file is empty.")

    if user_context and user_context.strip():
        user_content = (
            f"Informasi dari pengguna tentang meeting ini:\n{user_context.strip()}"
            f"\n\n---\n\nTranskrip:\n\n{text}"
        )
    else:
        user_content = f"Transkrip:\n\n{text}"

    tools = [types.Tool(google_search=types.GoogleSearch())] if SUMMARY_WEB_SEARCH else []
    active_model = SUMMARY_WEB_SEARCH_MODEL if SUMMARY_WEB_SEARCH else SUMMARY_MODEL

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=8192,
        tools=tools or None,
        # Thinking mode — internal reasoning improves quality; not shown in output
        thinking_config=types.ThinkingConfig(thinking_budget=8192),
    )

    logger.info(
        "Summarizing %s (%d chars) model=%s ctx=%s web_search=%s",
        txt_path, len(text), active_model, bool(user_context), SUMMARY_WEB_SEARCH,
    )

    # Show initial status
    if on_status:
        if SUMMARY_WEB_SEARCH:
            await on_status("🔍 Mencari informasi di web…")
        else:
            await on_status("🧠 Berpikir…")

    # Background ticker — updates draft every 3s during the silent thinking/search phase
    # so the user sees elapsed time instead of a frozen bubble.
    loop = asyncio.get_event_loop()
    start_ts = loop.time()
    ticker_task: asyncio.Task | None = None

    async def _tick() -> None:
        while True:
            await asyncio.sleep(3)
            if on_status:
                elapsed = int(loop.time() - start_ts)
                if SUMMARY_WEB_SEARCH:
                    await on_status(f"🔍 Mencari informasi di web… ({elapsed}s)")
                else:
                    await on_status(f"🧠 Berpikir… ({elapsed}s)")

    if on_status:
        ticker_task = asyncio.create_task(_tick())

    result_parts: list[str] = []
    text_started = False

    try:
        async for chunk in await client.aio.models.generate_content_stream(
            model=active_model,
            contents=user_content,
            config=config,
        ):
            if chunk.text:
                if not text_started:
                    text_started = True
                    # Stop ticker — text is flowing now
                    if ticker_task and not ticker_task.done():
                        ticker_task.cancel()
                        ticker_task = None
                    if on_status:
                        await on_status("✍️ Membuat ringkasan…")
                result_parts.append(chunk.text)
                if on_text_chunk:
                    await on_text_chunk("".join(result_parts))
    finally:
        if ticker_task and not ticker_task.done():
            ticker_task.cancel()

    return "".join(result_parts)


async def extract_context_gaps(txt_path: str) -> list[str]:
    """Scan transcript for missing context / ambiguities using a cheap fast model.

    Returns up to 5 clarification questions, or [] if context is clear or on any error.
    Only scans the first 10,000 chars to keep it fast.
    """
    if not GEMINI_API_KEY:
        return []
    try:
        text = Path(txt_path).read_text(encoding="utf-8").strip()
    except Exception:
        return []
    if not text:
        return []

    sample = text[:10_000]

    try:
        client = _get_client()
        response = await client.aio.models.generate_content(
            model=SUMMARY_PREPROCESS_MODEL,
            contents=_PREPROCESS_PROMPT + sample,
            config=types.GenerateContentConfig(
                max_output_tokens=512,
                temperature=0.1,
            ),
        )
        raw = response.text.strip()
        logger.info("Context gap raw response: %s", raw[:200])
        # Strip markdown code fences if model wrapped output
        clean = re.sub(r'```(?:json)?\s*(.*?)\s*```', r'\1', raw, flags=re.DOTALL).strip()
        # Greedy match: first '[' to last ']' — handles brackets inside question text
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        if match:
            items = json.loads(match.group())
            result = [str(q) for q in items[:5] if q]
            logger.info("Context gaps found: %s", result)
            return result
        logger.info("Context gap: no JSON array found in response")
    except Exception as e:
        logger.warning("Context gap extraction failed: %s", e)
    return []


def is_configured() -> bool:
    return bool(GEMINI_API_KEY)
