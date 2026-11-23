"""Text-to-speech through the `tts` capability binding.

Provider kinds: `test` (a short silent wav for tests),
`openai`/`openai-compatible` (POST /audio/speech — OpenAI or compatible
servers like openedai-speech / Kokoro-FastAPI).

`spoken_version` turns an assistant reply into speech-ready text: markdown
is stripped and overlong answers are compressed by the `chat_fast`
capability, so the voice stays laconic while the full text stays in chat.
"""

import io
import logging
import re
import wave

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

TTS_TEXT_MAX = 4000
# Above this length the spoken version is compressed through chat_fast.
SPEECH_SHORT_MAX = 600
_SPEECH_INPUT_MAX = 8000

_SPEECH_INSTRUCTIONS = (
    "Сожми текст для озвучивания голосом до 1-2 коротких предложений: "
    "сохрани суть, имена, числа и даты; без markdown, списков и ссылок. "
    "Ответь только текстом для произнесения."
)


async def synthesize_speech(
    session: AsyncSession, user: User, text: str
) -> tuple[bytes, str] | None:
    """Synthesize `text` to (audio bytes, mime); None when no TTS is bound."""
    # Lazy: concierge.agent imports the media pipeline at module load.
    from concierge.agent.providers import http_base_url, provider_api_key
    from concierge.agent.service import get_capability_binding

    binding = await get_capability_binding(session, "tts", user.role_id)
    if binding is None:
        return None
    provider = binding.provider
    if provider is None or not provider.enabled:
        return None
    kind = provider.kind
    if kind == "test":
        return _test_wav(), "audio/wav"
    api_key = await provider_api_key(session, user, provider)
    if kind in ("openai", "openai-compatible"):
        voice = str(binding.params.get("voice") or "alloy")
        fmt = str(binding.params.get("response_format") or "wav")
        return await _openai_speech(
            http_base_url(provider), binding.model, voice, api_key,
            text, fmt,
        )
    log.warning("tts: provider kind %r unsupported", kind)
    return None


_MIME_BY_FORMAT = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/L16",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
}


async def _openai_speech(
    base_url: str | None,
    model: str,
    voice: str,
    api_key: str | None,
    text: str,
    fmt: str,
) -> tuple[bytes, str]:
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{base}/audio/speech",
            json={
                "model": model,
                "input": text,
                "voice": voice,
                "response_format": fmt,
            },
            headers=headers,
        )
        resp.raise_for_status()
        return resp.content, _MIME_BY_FORMAT.get(fmt, f"audio/{fmt}")


_FENCED_RE = re.compile(r"```.*?```|```.*\Z", re.DOTALL)
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_MD_LINE_RE = re.compile(r"(?m)^\s{0,3}(?:#{1,6}|>|[-*+]|\d+[.)])\s+")
_MD_MARKS_RE = re.compile(r"(\*\*|__|~~)(.*?)\1|(\*|_)(.*?)\3")
_WS_RE = re.compile(r"\s+")
# Маркеры-вложения вида [reply.wav] / [голосовой ответ], которые модель
# иногда копирует из сериализованной истории ([label] для аудио-партов).
_ATT_MARKER_RE = re.compile(
    r"\[(?:голосовой ответ[^\]\n]*|[^\]\n]{0,80}\."
    r"(?:wav|mp3|ogg|m4a|opus|flac|aac|webm|mp4|mov|png|jpe?g|pdf))\]",
    re.IGNORECASE,
)
_BLANKS_RE = re.compile(r"\n{3,}")


def sanitize_reply_text(text: str) -> str:
    """Убрать эхо маркеров-вложений из текста ответа ассистента."""
    if not _ATT_MARKER_RE.search(text):
        return text
    text = _ATT_MARKER_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return _BLANKS_RE.sub("\n\n", text).strip()


def speechify(text: str) -> str:
    """Markdown-ish text -> plain spoken text (no code, links or markup)."""
    text = _FENCED_RE.sub(" ", text)
    text = _IMG_RE.sub(" ", text)
    text = _ATT_MARKER_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" ", text)
    text = _MD_LINE_RE.sub("", text)
    text = _MD_MARKS_RE.sub(lambda m: m.group(2) or m.group(4) or "", text)
    text = text.replace("`", "")
    return _WS_RE.sub(" ", text).strip()


def _truncate_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = max(text.rfind(p, 0, limit) for p in ".!?…")
    return text[: cut + 1].strip() if cut > limit // 2 else text[:limit].strip()


async def spoken_version(session: AsyncSession, user: User, text: str) -> str:
    """Reply text -> speech-ready version: plain text, short when long."""
    plain = speechify(text)
    if len(plain) <= SPEECH_SHORT_MAX:
        return plain
    from concierge.agent import providers

    bound = await providers.get_model(session, "chat_fast", user)
    if bound is not None:
        try:
            from pydantic_ai import Agent

            agent = Agent(
                bound.model,
                instructions=_SPEECH_INSTRUCTIONS,
                model_settings=bound.settings,
            )
            result = await agent.run(plain[:_SPEECH_INPUT_MAX])
            short = speechify(str(result.output))
            if short:
                return short[:TTS_TEXT_MAX]
        except Exception:
            log.exception("spoken_version compression failed")
    return _truncate_sentence(plain, SPEECH_SHORT_MAX)


def _test_wav() -> bytes:
    """0.25 s of silence — a valid wav for pipeline/UI tests."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * (16000 // 4))
    return buf.getvalue()
