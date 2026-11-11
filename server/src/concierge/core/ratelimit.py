"""Single-worker in-memory sliding-window rate limits.

Used for brute-force-sensitive paths: web login, telegram link-code
generation, telegram pairing attempts. Single uvicorn worker keeps an
in-process limiter sufficient (see roadmap: no multi-worker deploy).
"""

import time
from collections import deque

from fastapi import Request

# login: per (ip, username) failures + per-IP attempts
LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW = 600.0
LOGIN_IP_LIMIT = 60
LOGIN_IP_WINDOW = 60.0

# telegram link-code generation per concierge user
LINK_CODE_LIMIT = 5
LINK_CODE_WINDOW = 600.0

# telegram pairing attempts per telegram user id
TG_PAIR_LIMIT = 10
TG_PAIR_WINDOW = 600.0


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, hits: deque[float], now: float, window: float) -> None:
        while hits and now - hits[0] > window:
            hits.popleft()

    def allow(self, key: str, limit: int, window: float) -> bool:
        """Record one hit; False when `limit` hits already within `window`."""
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        self._prune(hits, now, window)
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def check(self, key: str, limit: int, window: float) -> bool:
        """Read-only check without recording a hit."""
        now = time.monotonic()
        hits = self._hits.get(key)
        if not hits:
            return True
        self._prune(hits, now, window)
        return len(hits) < limit

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def clear(self) -> None:
        self._hits.clear()


_limiter = RateLimiter()


def get_limiter() -> RateLimiter:
    return _limiter


def client_ip(request: Request) -> str:
    """Client IP behind the single trusted Caddy proxy."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""
