import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-sonnet-4-6")
SUMMARY_WEB_SEARCH = os.environ.get("SUMMARY_WEB_SEARCH", "true").lower() not in ("0", "false", "no")

_async_client: anthropic.AsyncAnthropic | None = None

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
- PDP: Pelindungan Data Pribadi
- KSS: Keamanan Siber dan Sandi

Struktur responmu persis seperti ini:

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

StatusCallback = Callable[[str], Awaitable[None]]


def _get_client() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is not set.")
        _async_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _async_client


async def summarize(
    txt_path: str,
    user_context: str | None = None,
    on_status: StatusCallback | None = None,
    on_text_chunk: StatusCallback | None = None,
) -> str:
    """Async streaming summarization with prompt caching.

    Callbacks:
      on_status(text)      — called when a web search starts or text begins after a search
      on_text_chunk(text)  — called with the accumulated text so far as each delta arrives;
                             caller is responsible for throttling Telegram edits
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

    # ── Prompt caching — system prompt is static, transcript rarely changes ──
    system_with_cache = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": user_content,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]

    create_kwargs: dict = dict(
        model=SUMMARY_MODEL,
        max_tokens=2048,
        system=system_with_cache,
        messages=messages,
    )
    if SUMMARY_WEB_SEARCH:
        create_kwargs["tools"] = [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
        ]

    logger.info(
        "Summarizing %s (%d chars) model=%s ctx=%s web_search=%s",
        txt_path, len(text), SUMMARY_MODEL, bool(user_context), SUMMARY_WEB_SEARCH,
    )

    result_parts: list[str] = []
    search_count = 0
    text_started = False
    current_block_type: str | None = None

    async with client.messages.stream(**create_kwargs) as stream:
        async for event in stream:
            etype = event.type  # type: ignore[attr-defined]

            if etype == "content_block_start":
                block = event.content_block  # type: ignore[attr-defined]
                current_block_type = block.type

                if block.type == "tool_use" and getattr(block, "name", "") == "web_search":
                    search_count += 1
                    if on_status:
                        await on_status(f"🔍 Mencari informasi di web… (#{search_count})")

                elif block.type == "text" and not text_started:
                    text_started = True
                    if on_status and search_count > 0:
                        await on_status("✍️ Membuat ringkasan…")

            elif etype == "content_block_delta":
                delta = event.delta  # type: ignore[attr-defined]
                if current_block_type == "text" and hasattr(delta, "text"):
                    result_parts.append(delta.text)
                    if on_text_chunk:
                        await on_text_chunk("".join(result_parts))

            elif etype == "content_block_stop":
                current_block_type = None

    return "".join(result_parts)


def is_configured() -> bool:
    return bool(ANTHROPIC_API_KEY)
