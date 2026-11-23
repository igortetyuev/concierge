"""Text-to-speech through the `tts` capability binding.

Provider kinds: `test` (a short silent wav for tests),
`openai`/`openai-compatible` (POST /audio/speech — OpenAI or compatible
servers like openedai-speech / Kokoro-FastAPI).
"""

import io
import logging
import wave

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)


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


def _test_wav() -> bytes:
    """0.25 s of silence — a valid wav for pipeline/UI tests."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * (16000 // 4))
    return buf.getvalue()
