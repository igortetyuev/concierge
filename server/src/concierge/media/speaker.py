"""Speaker-ID through the `speaker` capability binding.

Provider kinds: `test` (binding.params["embedding"] — canned vector for
tests) and `speaker-http` (POST {base}/embed, multipart wav →
{"embedding": [...]} — the speakerid sidecar). Errors degrade to a
warning + None: the pipeline never fails because of speaker-ID.

Every recognized clip either matches a `Voice` centroid
(>= `match_threshold`) or seeds a new one («Голос N»). Samples are stored
in `voice_samples` (UNIQUE by attachment — re-runs update, not duplicate);
the centroid is recomputed only on confident matches
(>= `learn_threshold`) or by admin actions (merge/add/remove samples).
"""

import logging
import math
import tempfile
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.core.db import utcnow
from concierge.media.models import Attachment, Voice, VoiceSample

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_MAX_SAMPLES = 300

_MATCH_THRESHOLD = 0.45
_LEARN_THRESHOLD = 0.60
MIN_DURATION_S = 1.0


async def embed_audio(
    session: AsyncSession, user: User, wav_path: Path
) -> tuple[list[float] | None, dict[str, Any]]:
    """Embedding of a normalized wav + the binding's params (thresholds).

    (None, params) when the capability is unbound or the provider call
    fails — caller just skips speaker data.
    """
    # Lazy: concierge.agent imports the media pipeline at module load.
    from concierge.agent.providers import http_base_url, provider_api_key
    from concierge.agent.service import get_capability_binding

    binding = await get_capability_binding(session, "speaker", user.role_id)
    if binding is None:
        return None, {}
    provider = binding.provider
    if provider is None or not provider.enabled:
        return None, binding.params
    kind = provider.kind
    if kind == "test":
        emb = binding.params.get("embedding")
        if isinstance(emb, list) and emb:
            return [float(x) for x in emb], binding.params
        return None, binding.params
    if kind == "speaker-http":
        base = http_base_url(provider)
        if not base:
            return None, binding.params
        api_key = await provider_api_key(session, user, provider)
        try:
            vec = await _embed_http(base, api_key, wav_path)
        except Exception:
            log.warning("speaker: embed request failed", exc_info=True)
            vec = None
        return vec, binding.params
    log.warning("speaker: provider kind %r unsupported", kind)
    return None, binding.params


async def embed_attachment(
    session: AsyncSession, user: User, att: Attachment
) -> tuple[list[float] | None, dict[str, Any]]:
    """Embedding for a stored attachment: blob → 16kHz wav → provider.

    Used by the admin sample-add endpoint and the backfill CLI — callers
    that re-embed existing audio outside the pipeline.
    """
    # Lazy: pipeline imports this module at load time.
    from concierge.media import service
    from concierge.media.pipeline import _stage, _to_wav

    data = service.read_blob(att.sha256)
    with tempfile.TemporaryDirectory() as tmp:
        src = _stage(data, tmp, att.original_name)
        try:
            wav = await _to_wav(src, Path(tmp) / "audio.wav")
        except Exception:
            log.warning("speaker: wav conversion failed attachment=%s", att.id)
            return None, {}
        return await embed_audio(session, user, wav)


async def _embed_http(
    base_url: str, api_key: str | None, wav_path: Path
) -> list[float] | None:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/embed",
            files={"file": (wav_path.name, wav_path.read_bytes(), "audio/wav")},
            headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()
    emb = payload.get("embedding")
    if not isinstance(emb, list) or not emb:
        return None
    return [float(x) for x in emb]


def cosine(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / math.sqrt(na * nb) if na and nb else 0.0


async def assign_voice(
    session: AsyncSession,
    att: Attachment,
    vec: list[float],
    params: dict[str, Any],
) -> dict[str, Any]:
    """Assign att's embedding to a voice; returns the derived["speaker"] dict."""
    match_threshold = float(params.get("match_threshold", _MATCH_THRESHOLD))
    learn_threshold = float(params.get("learn_threshold", _LEARN_THRESHOLD))

    best: Voice | None = None
    best_score = -1.0
    for voice in (await session.execute(select(Voice))).scalars():
        if not voice.centroid:
            continue
        score = cosine(vec, voice.centroid)
        if score > best_score:
            best, best_score = voice, score

    if best is None or best_score < match_threshold:
        max_id = (await session.execute(select(func.max(Voice.id)))).scalar() or 0
        voice = Voice(label=f"Голос {max_id + 1}", centroid=[], sample_count=0)
        session.add(voice)
        await session.flush()
        score = max(best_score, 0.0)
        learn = True  # a fresh cluster is seeded by its first sample
    else:
        voice = best
        score = best_score
        learn = score >= learn_threshold

    moved_from = None
    sample = (
        await session.execute(
            select(VoiceSample).where(VoiceSample.attachment_id == att.id)
        )
    ).scalar_one_or_none()
    if sample is None:
        sample = VoiceSample(voice_id=voice.id, attachment_id=att.id)
        session.add(sample)
    else:
        moved_from = sample.voice_id if sample.voice_id != voice.id else None
        sample.voice_id = voice.id
    sample.embedding = vec
    sample.score = score

    voice.last_heard_at = utcnow()
    if learn or voice.sample_attachment_id is None:
        voice.sample_attachment_id = att.id

    await _cap_samples(session, voice.id)
    if learn:
        await session.flush()
        await recompute_centroid(session, voice.id)
    else:
        await session.flush()
        voice.sample_count = await _count_samples(session, voice.id)
    if moved_from is not None:
        await recompute_centroid(session, moved_from)

    name = voice.label
    if voice.user_id is not None:
        owner = await session.get(User, voice.user_id)
        if owner is not None:
            name = owner.display_name
    return {
        "voice_id": voice.id,
        "user_id": voice.user_id,
        "name": name,
        "score": round(score, 4),
    }


async def recompute_centroid(session: AsyncSession, voice_id: int) -> Voice | None:
    """centroid = L2-normalized mean of the stored samples; syncs sample_count."""
    voice = await session.get(Voice, voice_id)
    if voice is None:
        return None
    embs = [
        e
        for e in (
            await session.execute(
                select(VoiceSample.embedding).where(VoiceSample.voice_id == voice_id)
            )
        ).scalars()
        if e
    ]
    voice.sample_count = len(embs)
    if not embs:
        voice.centroid = []
        return voice
    dim = len(embs[0])
    acc = [0.0] * dim
    n = 0
    for emb in embs:
        if len(emb) != dim:
            continue
        for i, x in enumerate(emb):
            acc[i] += float(x)
        n += 1
    mean = [x / n for x in acc]
    norm = math.sqrt(sum(x * x for x in mean))
    voice.centroid = [x / norm for x in mean] if norm else mean
    return voice


async def _count_samples(session: AsyncSession, voice_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(VoiceSample).where(
                VoiceSample.voice_id == voice_id
            )
        )
    ).scalar_one()


async def _cap_samples(session: AsyncSession, voice_id: int) -> None:
    keep = (
        select(VoiceSample.id)
        .where(VoiceSample.voice_id == voice_id)
        .order_by(VoiceSample.id.desc())
        .limit(_MAX_SAMPLES)
    )
    await session.execute(
        delete(VoiceSample).where(
            VoiceSample.voice_id == voice_id, VoiceSample.id.not_in(keep)
        )
    )
