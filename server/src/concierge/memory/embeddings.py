import hashlib
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from concierge.agent.providers import http_base_url, provider_api_key
from concierge.agent.service import get_capability_binding
from concierge.auth.models import User

log = logging.getLogger(__name__)

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "ollama": "http://localhost:11434/v1",
}

TEST_DIM = 64

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


@dataclass(slots=True)
class BoundEmbedder:
    """A resolved `embedding` capability: embed fn + collection suffix."""

    embed: EmbedFn
    slug: str  # model-derived collection suffix, e.g. "allminilml6v2"
    label: str


def model_slug(model_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", model_name.lower())
    return slug or "default"


async def _test_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic bag-of-token vectors for tests (no external calls).

    Shared tokens produce positive cosine similarity — enough to exercise
    the retrieval path without a real provider.
    """
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * TEST_DIM
        for tok in re.findall(r"\w+", text.lower()):
            digest = hashlib.sha1(tok.encode()).digest()
            vec[digest[0] % TEST_DIM] += 1.0 if digest[1] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        out.append([v / norm for v in vec] if norm else vec)
    return out


def _http_embed(
    base_url: str, model: str, api_key: str | None
) -> EmbedFn:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = f"{base_url.rstrip('/')}/embeddings"

    async def embed(texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url, headers=headers, json={"model": model, "input": texts}
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        by_index = {item["index"]: item["embedding"] for item in data}
        return [by_index[i] for i in range(len(texts))]

    return embed


async def get_embedder(
    session: AsyncSession, user: User
) -> BoundEmbedder | None:
    """Resolve the `embedding` capability for `user` (role binding wins)."""
    binding = await get_capability_binding(session, "embedding", user.role_id)
    if binding is None:
        return None
    provider = binding.provider
    if provider is None or not provider.enabled:
        return None
    label = f"{provider.name}/{binding.model}"
    slug = model_slug(binding.model)
    if provider.kind == "test":
        return BoundEmbedder(_test_embed, slug, label)
    base_url = http_base_url(provider) or DEFAULT_BASE_URLS.get(
        provider.kind, ""
    )
    if not base_url:
        log.warning("embedding provider %r has no base_url", provider.name)
        return None
    api_key = await provider_api_key(session, user, provider)
    return BoundEmbedder(
        _http_embed(base_url, binding.model, api_key), slug, label
    )
