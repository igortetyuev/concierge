"""Speech-to-text through the `stt` capability binding.

Provider kinds: `test` (canned transcript for tests), `deepgram`
(POST /v1/listen), `openai`/`openai-compatible` (POST /audio/transcriptions —
whisper-compatible servers like OpenAI or faster-whisper-server).
"""

import logging
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


async def transcribe_audio(
    session: AsyncSession, user: User, wav_path: Path
) -> str | None:
    """Transcribe a normalized wav file; None when no STT provider is bound."""
    # Lazy: concierge.agent imports the media pipeline at module load.
    from concierge.agent.providers import resolve_ref
    from concierge.agent.service import get_capability_binding

    binding = await get_capability_binding(session, "stt", user.role_id)
    if binding is None:
        return None
    provider = binding.provider
    if provider is None or not provider.enabled:
        return None
    kind = provider.kind
    if kind == "test":
        if "transcript" in binding.params:
            return str(binding.params["transcript"])
        return "test transcript"
    api_key = await resolve_ref(session, user, provider.api_key_ref)
    if kind == "deepgram":
        return await _deepgram(
            provider.base_url or "https://api.deepgram.com",
            binding.model,
            binding.params.get("language"),
            api_key,
            wav_path,
        )
    if kind in ("openai", "openai-compatible"):
        return await _openai_whisper(
            provider.base_url, binding.model, binding.params.get("language"), api_key, wav_path
        )
    log.warning("stt: provider kind %r unsupported", kind)
    return None


async def _deepgram(
    base_url: str,
    model: str,
    language: object,
    api_key: str | None,
    wav_path: Path,
) -> str:
    params: dict[str, str] = {"model": model, "smart_format": "true"}
    if language:
        params["language"] = str(language)
    headers = {"Content-Type": "audio/wav"}
    if api_key:
        headers["Authorization"] = f"Token {api_key}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/listen",
            params=params,
            headers=headers,
            content=wav_path.read_bytes(),
        )
        resp.raise_for_status()
        data = resp.json()
    channels = data.get("results", {}).get("channels", [])
    for ch in channels:
        for alt in ch.get("alternatives", []):
            text = str(alt.get("transcript") or "").strip()
            if text:
                return text
    return ""


async def _openai_whisper(
    base_url: str | None,
    model: str,
    language: object,
    api_key: str | None,
    wav_path: Path,
) -> str:
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    data: dict[str, str] = {"model": model}
    if language:
        data["language"] = str(language)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{base}/audio/transcriptions",
            data=data,
            files={"file": (wav_path.name, wav_path.read_bytes(), "audio/wav")},
            headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()
    return str(payload.get("text") or "").strip()
