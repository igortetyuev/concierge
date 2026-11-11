import logging
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

from concierge.core.config import get_settings

log = logging.getLogger(__name__)

MEMORY_PREFIX = "concierge_memory__"
MESSAGES_PREFIX = "concierge_messages__"

_client: AsyncQdrantClient | None = None
_ensured: set[str] = set()


def get_qdrant() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(url=get_settings().qdrant_url)
    return _client


def set_qdrant(client: AsyncQdrantClient | None) -> None:
    """Replace the global client (tests)."""
    global _client
    _client = client
    _ensured.clear()


def memory_collection(slug: str) -> str:
    return f"{MEMORY_PREFIX}{slug}"


def messages_collection(slug: str) -> str:
    return f"{MESSAGES_PREFIX}{slug}"


async def ensure_collections(
    client: AsyncQdrantClient, slug: str, dim: int
) -> None:
    """Create the two collections for this embedder model, once per process.

    A model change yields a new slug, hence new collections (reindex follows).
    Payload indexes are no-ops in local mode but required on the server.
    """
    for name, indexes in (
        (
            memory_collection(slug),
            {
                "scope": qm.PayloadSchemaType.KEYWORD,
                "owner_id": qm.PayloadSchemaType.INTEGER,
            },
        ),
        (
            messages_collection(slug),
            {
                "conversation_id": qm.PayloadSchemaType.INTEGER,
                "author_id": qm.PayloadSchemaType.INTEGER,
            },
        ),
    ):
        if name in _ensured:
            continue
        if not await client.collection_exists(name):
            await client.create_collection(
                name,
                vectors_config=qm.VectorParams(
                    size=dim, distance=qm.Distance.COSINE
                ),
            )
        for field, schema in indexes.items():
            await client.create_payload_index(
                name, field, field_schema=schema
            )
        _ensured.add(name)


def _user_memory_filter(user_id: int) -> qm.Filter:
    """Items the user may see: own personal memory + shared family memory."""
    return qm.Filter(
        should=[
            qm.FieldCondition(
                key="owner_id", match=qm.MatchValue(value=user_id)
            ),
            qm.FieldCondition(key="scope", match=qm.MatchValue(value="family")),
        ]
    )


async def upsert_memory(
    client: AsyncQdrantClient,
    slug: str,
    item_id: int,
    vector: list[float],
    payload: dict[str, Any],
) -> None:
    await client.upsert(
        memory_collection(slug),
        points=[
            qm.PointStruct(id=item_id, vector=vector, payload=payload)
        ],
    )


async def delete_memory(
    client: AsyncQdrantClient, slug: str, item_ids: list[int]
) -> None:
    ids: list[int | str | uuid.UUID] = list(item_ids)
    await client.delete(
        memory_collection(slug),
        points_selector=qm.PointIdsList(points=ids),
    )


async def search_memory(
    client: AsyncQdrantClient,
    slug: str,
    vector: list[float],
    user_id: int,
    limit: int,
) -> list[qm.ScoredPoint]:
    res = await client.query_points(
        memory_collection(slug),
        query=vector,
        query_filter=_user_memory_filter(user_id),
        limit=limit,
        with_payload=True,
    )
    return list(res.points)


async def upsert_messages(
    client: AsyncQdrantClient,
    slug: str,
    points: list[qm.PointStruct],
) -> None:
    await client.upsert(messages_collection(slug), points=points)


async def delete_message_points(
    client: AsyncQdrantClient, message_ids: list[int]
) -> None:
    """Drop indexed message vectors from every messages collection.

    The embedder slug may have changed since indexing, so sweep all
    concierge_messages__* collections rather than guessing one.
    """
    ids: list[int | str | uuid.UUID] = list(message_ids)
    for c in (await client.get_collections()).collections:
        if c.name.startswith(MESSAGES_PREFIX):
            await client.delete(
                c.name, points_selector=qm.PointIdsList(points=ids)
            )


async def search_messages(
    client: AsyncQdrantClient,
    slug: str,
    vector: list[float],
    conversation_ids: list[int],
    limit: int,
    exclude_conversation_id: int | None = None,
) -> list[qm.ScoredPoint]:
    if not conversation_ids:
        return []
    flt = qm.Filter(
        must=[
            qm.FieldCondition(
                key="conversation_id",
                match=qm.MatchAny(any=conversation_ids),
            )
        ]
    )
    if exclude_conversation_id is not None:
        flt.must_not = [
            qm.FieldCondition(
                key="conversation_id",
                match=qm.MatchValue(value=exclude_conversation_id),
            )
        ]
    res = await client.query_points(
        messages_collection(slug),
        query=vector,
        query_filter=flt,
        limit=limit,
        with_payload=True,
    )
    return list(res.points)
