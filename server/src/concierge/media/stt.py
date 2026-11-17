"""Speech-to-text through the `stt` capability binding.

Provider kinds: `test` (canned transcript for tests), `deepgram`
(POST /v1/listen), `openai`/`openai-compatible` (POST /audio/transcriptions —
whisper-compatible servers like OpenAI or faster-whisper-server).
"""

import logging
import re
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)

# Whisper-family models recite subtitle credits and channel plugs when fed
# silence/noise ("Субтитры сделал DimaTorzok" is the classic). Transcripts that
# consist solely of such artifacts — compared lowercase, alphanumeric-only —
# are dropped as if nothing was said.
_HALLUCINATION_PHRASES = frozenset({"you", "dimatorzok"})
_HALLUCINATION_PREFIXES = (
    "субтитрысделал",
    "субтитрыделал",
    "субтитрысоздавал",
    "субтитрыподогнал",
    "субтитрыавтор",
    "редакторсубтитров",
    "переводсубтитров",
    "корректора",
    "спасибозапросмотр",
    "продолжениеследует",
    "подписывайтесьнаканал",
    "подписывайтесьнамойканал",
    "thanksforwatching",
    "thankyouforwatching",
    "pleasesubscribe",
    "subscribetomychannel",
    "tobecontinued",
)
_SEGMENT_SPLIT = re.compile(r"[.!?\n\r…]+")
# Initials ("A.Sinetskaya") must not split credit segments at the period.
_INITIALS = re.compile(r"\b([^\W\d_])\.(?=\s*[^\W\d_])")
# On faint speech/noise the decoder loops a single token dozens of times
# ("Кнопка 4, 5, 5, 1, 1, 1, 1, ..."). No real utterance has a run this long.
_TOKEN = re.compile(r"\w+")
_REPEAT_RUN = 20


def _normalized(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _known_artifact(seg: str) -> bool:
    return seg in _HALLUCINATION_PHRASES or seg.startswith(_HALLUCINATION_PREFIXES)


def _repetition_loop(text: str) -> bool:
    run = 1
    prev = None
    for tok in _TOKEN.findall(text.lower()):
        run = run + 1 if tok == prev else 1
        if run >= _REPEAT_RUN:
            return True
        prev = tok
    return False


def _hallucinated(text: str) -> bool:
    """True when the transcript carries no real speech: either no words at all
    (punctuation/notes only), a decoder repetition loop, or every segment is
    a known noise artifact."""
    if _repetition_loop(text):
        return True
    segments = [
        s
        for s in (
            _normalized(x)
            for x in _SEGMENT_SPLIT.split(_INITIALS.sub(r"\1", text))
        )
        if s
    ]
    return not segments or all(_known_artifact(s) for s in segments)


async def transcribe_audio(
    session: AsyncSession, user: User, wav_path: Path
) -> str | None:
    """Transcribe a normalized wav file; None when no STT provider is bound."""
    # Lazy: concierge.agent imports the media pipeline at module load.
    from concierge.agent.providers import http_base_url, provider_api_key
    from concierge.agent.service import get_capability_binding

    binding = await get_capability_binding(session, "stt", user.role_id)
    if binding is None:
        return None
    provider = binding.provider
    if provider is None or not provider.enabled:
        return None
    kind = provider.kind
    if kind == "test":
        text = str(binding.params.get("transcript", "test transcript"))
    else:
        api_key = await provider_api_key(session, user, provider)
        if kind == "deepgram":
            text = await _deepgram(
                http_base_url(provider) or "https://api.deepgram.com",
                binding.model,
                binding.params.get("language"),
                api_key,
                wav_path,
            )
        elif kind in ("openai", "openai-compatible"):
            text = await _openai_whisper(
                http_base_url(provider),
                binding.model,
                binding.params.get("language"),
                api_key,
                wav_path,
            )
        else:
            log.warning("stt: provider kind %r unsupported", kind)
            return None
    if _hallucinated(text):
        if text.strip():
            log.warning("stt: dropped noise transcript %r", text.strip()[:120])
        return ""
    return text


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
