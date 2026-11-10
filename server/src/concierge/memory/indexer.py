import asyncio
import contextlib
import logging
import uuid
from typing import Any

from pydantic_ai import Agent
from qdrant_client import models as qm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent import providers
from concierge.agent.tools import message_text
from concierge.auth.models import User
from concierge.conversations.models import Conversation, Message
from concierge.core.db import get_sessionmaker, utcnow
from concierge.memory import store
from concierge.memory.embeddings import get_embedder

log = logging.getLogger(__name__)

BATCH_SIZE = 50
SWEEP_INTERVAL_SECONDS = 60.0
PAYLOAD_TEXT_MAX = 2000
SUMMARY_MESSAGES = 30
SUMMARY_INPUT_CHARS = 4000
SUMMARY_MAX_CHARS = 2000

SUMMARY_INSTRUCTIONS = (
    "Сожми диалог в краткое содержание (3-5 предложений, по-русски): темы, "
    "решения, важные факты. Только текст, без заголовков."
)

# UUID namespace for synthetic points (e.g. conversation summaries) in the
# messages collection — real points use integer message ids.
SUMMARY_NS = uuid.UUID("6f5e0f8a-9f1b-4c2a-b8d4-3a2c1e0f9d7b")

_worker: asyncio.Task[None] | None = None
_worker_loop: asyncio.AbstractEventLoop | None = None
_kick: asyncio.Event | None = None
_jobs: set[asyncio.Task[Any]] = set()


def kick() -> None:
    """Nudge the background indexer from any thread/loop; no-op when the
    worker isn't running (e.g. in tests)."""
    if _worker is not None and _worker_loop is not None and _kick is not None:
        _worker_loop.call_soon_threadsafe(_kick.set)


def start_worker() -> None:
    global _worker, _worker_loop
    if _worker is None:
        _worker_loop = asyncio.get_running_loop()
        _worker = asyncio.create_task(_loop())


async def stop_worker() -> None:
    global _worker, _worker_loop, _kick
    if _worker is not None:
        _worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _worker
        _worker = None
    _worker_loop = None
    _kick = None


async def wait_idle() -> None:
    """Drain archive jobs and pending outbox rows (tests, shutdown)."""
    while _jobs:
        await asyncio.gather(*_jobs)
    while await index_pending():
        pass


async def _loop() -> None:
    global _kick
    _kick = asyncio.Event()
    while True:
        _kick.clear()
        try:
            await index_pending()
        except Exception:
            log.exception("memory index sweep failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_kick.wait(), timeout=SWEEP_INTERVAL_SECONDS)


def schedule_archive(conversation_id: int) -> None:
    task = asyncio.create_task(archive_and_index(conversation_id))
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)


async def archive_and_index(conversation_id: int) -> None:
    """On archive: summarize the dialogue (chat_fast), then index messages
    and the summary so the archived session stays searchable."""
    session_maker = get_sessionmaker()
    async with session_maker() as session:
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            return
        owner = await session.get(User, conv.owner_id)
        if owner is None:
            return
        if conv.summary is None:
            conv.summary = await _summarize(session, conv, owner)
            await session.commit()
        embedder = await get_embedder(session, owner)
        slug = embedder.slug if embedder is not None else None
        summary = conv.summary
    await index_pending()
    if summary and slug is not None and embedder is not None:
        try:
            vectors = await embedder.embed([summary])
            client = store.get_qdrant()
            await store.ensure_collections(client, slug, len(vectors[0]))
            await store.upsert_messages(
                client,
                slug,
                [
                    qm.PointStruct(
                        id=str(uuid.uuid5(SUMMARY_NS, f"summary:{conversation_id}")),
                        vector=vectors[0],
                        payload={
                            "kind": "summary",
                            "conversation_id": conversation_id,
                            "text": summary[:PAYLOAD_TEXT_MAX],
                        },
                    )
                ],
            )
        except Exception:
            log.exception("summary indexing failed conv=%s", conversation_id)


async def _summarize(
    session: AsyncSession, conv: Conversation, owner: User
) -> str | None:
    bound = await providers.get_model(session, "chat_fast", owner)
    if bound is None:
        return None
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.seq.desc())
            .limit(SUMMARY_MESSAGES)
        )
    ).scalars()
    texts = [message_text(m.parts) for m in rows]
    joined = "\n".join(t for t in reversed(texts) if t)
    if not joined.strip():
        return None
    try:
        agent = Agent(
            bound.model,
            instructions=SUMMARY_INSTRUCTIONS,
            model_settings=bound.settings,
        )
        result = await agent.run(joined[:SUMMARY_INPUT_CHARS])
        return str(result.output).strip()[:SUMMARY_MAX_CHARS] or None
    except Exception:
        log.exception("summary generation failed conv=%s", conv.id)
        return None


async def index_pending(limit: int = BATCH_SIZE) -> int:
    """Index unindexed messages (the outbox) into Qdrant. Returns count."""
    session_maker = get_sessionmaker()
    async with session_maker() as session:
        rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.indexed_at.is_(None))
                    .order_by(Message.id)
                    .limit(limit)
                )
            ).scalars()
        )
        if not rows:
            return 0
        conv_ids = {m.conversation_id for m in rows}
        owners = dict(
            (
                await session.execute(
                    select(Conversation.id, Conversation.owner_id).where(
                        Conversation.id.in_(conv_ids)
                    )
                )
            ).all()
        )
        user_ids = {
            m.author_id if m.author_id is not None else owners[m.conversation_id]
            for m in rows
        }
        users = {
            u.id: u
            for u in (
                await session.execute(select(User).where(User.id.in_(user_ids)))
            ).scalars()
        }
        groups: dict[int, list[Message]] = {}
        for m in rows:
            uid = (
                m.author_id
                if m.author_id is not None
                else owners[m.conversation_id]
            )
            groups.setdefault(uid, []).append(m)

        indexed = 0
        for uid, msgs in groups.items():
            user = users.get(uid)
            if user is None:
                for m in msgs:
                    m.indexed_at = utcnow()
                    indexed += 1
                continue
            embedder = await get_embedder(session, user)
            if embedder is None:
                continue  # binding may appear later; retry on the next sweep
            texts = [message_text(m.parts) for m in msgs]
            try:
                vectors = await embedder.embed(
                    [t[:PAYLOAD_TEXT_MAX] or " " for t in texts]
                )
            except Exception:
                log.exception("embedding batch failed user=%s", uid)
                continue
            client = store.get_qdrant()
            try:
                await store.ensure_collections(client, embedder.slug, len(vectors[0]))
                points = [
                    qm.PointStruct(
                        id=m.id,
                        vector=v,
                        payload=_message_payload(m, t),
                    )
                    for m, t, v in zip(msgs, texts, vectors, strict=True)
                    if t.strip()
                ]
                if points:
                    await store.upsert_messages(client, embedder.slug, points)
            except Exception:
                log.exception("qdrant upsert failed user=%s", uid)
                continue
            for m in msgs:
                m.indexed_at = utcnow()
                indexed += 1
        await session.commit()
        return indexed


def _message_payload(m: Message, text: str) -> dict[str, Any]:
    return {
        "kind": "message",
        "message_id": m.id,
        "conversation_id": m.conversation_id,
        "seq": m.seq,
        "author_kind": m.author_kind,
        "author_id": m.author_id,
        "text": text[:PAYLOAD_TEXT_MAX],
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }
