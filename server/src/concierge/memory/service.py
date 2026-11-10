import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.auth.models import User
from concierge.auth.permissions import has_permission
from concierge.conversations.models import Conversation, ConversationMember
from concierge.core.db import utcnow
from concierge.memory import store
from concierge.memory.embeddings import BoundEmbedder, get_embedder
from concierge.memory.models import MemoryItem
from concierge.settings import service as settings_service

log = logging.getLogger(__name__)

RECALL_LIMIT = 5
RECALL_MAX_CHARS = 1500


def can_write_family(user: User) -> bool:
    return has_permission(user.role.permissions, "family_memory")


async def _embed_and_ensure(
    session: AsyncSession, user: User, texts: list[str]
) -> tuple[BoundEmbedder, list[list[float]]] | None:
    embedder = await get_embedder(session, user)
    if embedder is None:
        return None
    vectors = await embedder.embed(texts)
    if not vectors or not vectors[0]:
        return None
    await store.ensure_collections(store.get_qdrant(), embedder.slug, len(vectors[0]))
    return embedder, vectors


async def remember(
    session: AsyncSession,
    user: User,
    text: str,
    scope: str = "user",
    source_message_id: int | None = None,
    kind: str = "note",
) -> MemoryItem:
    """Persist a memory item and index it in Qdrant (best effort)."""
    scope = "family" if scope == "family" else "user"
    if scope == "family" and not can_write_family(user):
        raise PermissionError("family_memory")
    item = MemoryItem(
        scope=scope,
        owner_id=user.id if scope == "user" else None,
        text=text,
        source_message_id=source_message_id,
        kind=kind,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    try:
        ctx = await _embed_and_ensure(session, user, [text])
        if ctx is not None:
            embedder, vectors = ctx
            await store.upsert_memory(
                store.get_qdrant(),
                embedder.slug,
                item.id,
                vectors[0],
                {
                    "memory_id": item.id,
                    "scope": item.scope,
                    "owner_id": item.owner_id,
                    "kind": item.kind,
                    "text": item.text[:2000],
                },
            )
    except Exception:
        log.exception("memory item %s not indexed", item.id)
    return item


async def recall(
    session: AsyncSession, user: User, query: str, limit: int = RECALL_LIMIT
) -> list[dict[str, Any]]:
    """Semantic search over items visible to `user` (own + family)."""
    if not query.strip():
        return []
    try:
        ctx = await _embed_and_ensure(session, user, [query])
    except Exception:
        log.exception("recall embedding failed")
        return []
    if ctx is None:
        return []
    embedder, vectors = ctx
    hits = await store.search_memory(
        store.get_qdrant(), embedder.slug, vectors[0], user.id, limit
    )
    return [
        {
            "id": p.id,
            "text": (p.payload or {}).get("text", ""),
            "scope": (p.payload or {}).get("scope", ""),
            "kind": (p.payload or {}).get("kind", ""),
            "score": round(p.score, 4),
        }
        for p in hits
    ]


async def forget(session: AsyncSession, user: User, memory_id: int) -> bool:
    """Soft-delete an item the user may manage; drop its vector."""
    item = await session.get(MemoryItem, memory_id)
    if item is None or item.deleted_at is not None:
        return False
    if item.scope == "family":
        if not can_write_family(user):
            raise PermissionError("family_memory")
    elif item.owner_id != user.id and not has_permission(
        user.role.permissions, "*"
    ):
        raise PermissionError("not owner")
    item.deleted_at = utcnow()
    await session.commit()
    try:
        embedder = await get_embedder(session, user)
        if embedder is not None:
            await store.delete_memory(
                store.get_qdrant(), embedder.slug, [item.id]
            )
    except Exception:
        log.exception("memory item %s vector not deleted", item.id)
    return True


async def list_items(session: AsyncSession, user: User) -> list[MemoryItem]:
    """Own personal items plus all family items, newest first."""
    rows = (
        await session.execute(
            select(MemoryItem)
            .where(
                MemoryItem.deleted_at.is_(None),
                or_(
                    MemoryItem.scope == "family",
                    MemoryItem.owner_id == user.id,
                ),
            )
            .order_by(MemoryItem.created_at.desc())
            .limit(500)
        )
    ).scalars()
    return list(rows)


async def member_conversation_ids(
    session: AsyncSession, user_id: int
) -> list[int]:
    return list(
        (
            await session.execute(
                select(ConversationMember.conversation_id).where(
                    ConversationMember.user_id == user_id
                )
            )
        ).scalars()
    )


async def retrieve_context(
    session: AsyncSession, user: User, conv: Conversation, query: str
) -> str | None:
    """Build the [Память] prompt block: personal + family memory and
    relevant past-session snippets, filtered by the user's access."""
    if not query.strip():
        return None
    try:
        ctx = await _embed_and_ensure(session, user, [query])
    except Exception:
        log.exception("retrieval embedding failed conv=%s", conv.id)
        return None
    if ctx is None:
        return None
    embedder, vectors = ctx
    limit = int(
        await settings_service.get_value(
            session, user, "memory.recall_limit", RECALL_LIMIT
        )
    )
    max_chars = int(
        await settings_service.get_value(
            session, user, "memory.recall_max_chars", RECALL_MAX_CHARS
        )
    )
    client = store.get_qdrant()
    conv_ids = await member_conversation_ids(session, user.id)
    mem_hits = await store.search_memory(
        client, embedder.slug, vectors[0], user.id, limit
    )
    msg_hits = await store.search_messages(
        client,
        embedder.slug,
        vectors[0],
        conv_ids,
        limit,
        exclude_conversation_id=conv.id,
    )

    sections: list[str] = []
    personal = [
        str((h.payload or {}).get("text", ""))
        for h in mem_hits
        if (h.payload or {}).get("scope") == "user"
    ]
    family = [
        str((h.payload or {}).get("text", ""))
        for h in mem_hits
        if (h.payload or {}).get("scope") == "family"
    ]
    if personal:
        sections.append("Личные заметки:\n" + "\n".join(f"- {t}" for t in personal))
    if family:
        sections.append("Семейная память:\n" + "\n".join(f"- {t}" for t in family))
    if msg_hits:
        titles = await _conv_titles(
            session, {int((h.payload or {})["conversation_id"]) for h in msg_hits}
        )
        lines = []
        for h in msg_hits:
            p = h.payload or {}
            title = titles.get(int(p["conversation_id"]), "?")
            text = str(p.get("text", ""))[:500]
            if text:
                lines.append(f"- «{title}»: {text}")
        if lines:
            sections.append("Из прошлых сессий:\n" + "\n".join(lines))
    if not sections:
        return None
    return "\n\n".join(sections)[:max_chars]


async def _conv_titles(
    session: AsyncSession, conv_ids: set[int]
) -> dict[int, str]:
    if not conv_ids:
        return {}
    rows = (
        await session.execute(
            select(Conversation.id, Conversation.title).where(
                Conversation.id.in_(conv_ids)
            )
        )
    ).all()
    return {cid: title or f"#{cid}" for cid, title in rows}

