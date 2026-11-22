"""In-memory sliding-window rate limiter for sensitive endpoints."""

import asyncio
import json
import math
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Request, status

from app.core.exceptions import AppError


class InMemoryRateLimiter:
    """Thread/coroutine-safe sliding-window rate limiter."""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str, *, max_requests: int, window_seconds: int) -> None:
        """Record an attempt and raise 429 AppError if the limit is exceeded."""
        async with self._lock:
            now = time.monotonic()
            cutoff = now - window_seconds
            bucket = self._buckets[key]

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= max_requests:
                retry_after = max(1, math.ceil(bucket[0] + window_seconds - now))
                raise AppError(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    code="RATE_LIMIT_EXCEEDED",
                    message=(
                        "Muitas tentativas em sequência. "
                        "Aguarde alguns instantes e tente novamente."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )

            bucket.append(now)

    def reset(self) -> None:
        """Clear all recorded rate-limit buckets (for isolated tests)."""
        self._buckets.clear()


rate_limiter = InMemoryRateLimiter()


def _extract_client_ip(request: Request) -> str:
    """Extract client IP address safely, respecting X-Forwarded-For behind proxies."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        first_ip = forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def _extract_email_from_body(request: Request) -> str | None:
    """Safely inspect JSON body for an 'email' field without breaking validation."""
    raw_body = await request.body()
    if not raw_body:
        return None
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("email"), str):
        normalized = parsed["email"].strip().lower()
        return normalized or None
    return None


def rate_limit(
    *,
    scope: str,
    max_requests: int,
    window_seconds: int,
    include_email: bool = False,
) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency enforcing sliding-window rate limits."""

    async def _dependency(request: Request) -> None:
        client_ip = _extract_client_ip(request)
        key = f"{scope}:{client_ip}"
        if include_email:
            email = await _extract_email_from_body(request)
            if email:
                key = f"{key}:{email}"
        await rate_limiter.check(
            key,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )

    return _dependency


async def check_user_rate_limit(
    *,
    scope: str,
    user_id: UUID,
    max_requests: int,
    window_seconds: int,
) -> None:
    """Enforce sliding-window rate limit keyed by authenticated user ID."""
    await rate_limiter.check(
        f"{scope}:{user_id}",
        max_requests=max_requests,
        window_seconds=window_seconds,
    )


async def check_clip_view_rate_limit(
    *,
    request: Request,
    clip_id: UUID,
    user_id: UUID | None,
    window_seconds: int = 600,
) -> None:
    """Limit clip view increments to once per 10 minutes per (user or IP) + clip."""
    actor = str(user_id) if user_id is not None else _extract_client_ip(request)
    await rate_limiter.check(
        f"clip_view:{actor}:{clip_id}",
        max_requests=1,
        window_seconds=window_seconds,
    )
