"""Thread-safe and async-safe in-memory TTL cache with per-user invalidation."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

T = TypeVar("T")
CacheParams = tuple[tuple[str, str], ...]
CacheKey = tuple[UUID, str, CacheParams]
_InflightKey = tuple[int, CacheKey]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """Single cached payload paired with its monotonic expiration timestamp."""

    value: Any
    expires_at: float


def _stringify_param_value(value: Any) -> str:
    """Convert a query parameter value into a deterministic string representation."""
    if value is None:
        return "<none>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.strip()
    return str(value)


def normalize_cache_params(params: Mapping[str, Any] | None) -> CacheParams:
    """Canonicalize query parameters into a sorted, hashable tuple."""
    if not params:
        return ()
    return tuple(sorted((str(key), _stringify_param_value(value)) for key, value in params.items()))


def build_cache_key(
    user_id: UUID,
    endpoint: str,
    params: Mapping[str, Any] | None = None,
) -> CacheKey:
    """Build a deterministic cache key scoped by (user_id, endpoint, query_params)."""
    return (user_id, endpoint, normalize_cache_params(params))


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """Prevent asyncio 'Future exception was never retrieved' warnings on unawaited leaders."""
    if not future.cancelled():
        future.exception()


class UserScopedTTLCache:
    """Bounded in-memory TTL cache supporting per-user invalidation and in-flight coalescing."""

    def __init__(
        self,
        *,
        default_ttl_seconds: float = 30.0,
        max_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._default_ttl_seconds = default_ttl_seconds
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[CacheKey, _CacheEntry] = {}
        self._user_keys: dict[UUID, set[CacheKey]] = defaultdict(set)
        self._user_generations: dict[UUID, int] = defaultdict(int)
        self._inflight: dict[_InflightKey, asyncio.Future[Any]] = {}

    @property
    def default_ttl_seconds(self) -> float:
        """Return default entry time-to-live in seconds."""
        return self._default_ttl_seconds

    def get(
        self,
        user_id: UUID,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
    ) -> Any | None:
        """Return cached value if present and unexpired, otherwise None."""
        key = build_cache_key(user_id, endpoint, params)
        with self._lock:
            return self._get_unlocked(key)

    def set(
        self,
        user_id: UUID,
        endpoint: str,
        value: Any,
        *,
        params: Mapping[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        """Store a value under (user_id, endpoint, params) for the given TTL."""
        effective_ttl = self._resolve_ttl(ttl_seconds)
        if effective_ttl <= 0:
            return
        key = build_cache_key(user_id, endpoint, params)
        with self._lock:
            self._store_unlocked(key, value, ttl_seconds=effective_ttl)

    async def get_or_set(
        self,
        user_id: UUID,
        endpoint: str,
        loader: Callable[[], Awaitable[T]],
        *,
        params: Mapping[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> T:
        """Return cached value or coalesce concurrent loads for the same cache key."""
        effective_ttl = self._resolve_ttl(ttl_seconds)
        if effective_ttl <= 0:
            return await loader()

        key = build_cache_key(user_id, endpoint, params)
        loop = asyncio.get_running_loop()
        inflight_key: _InflightKey = (id(loop), key)

        hit, cached_value, shared_future, is_leader, start_gen = self._lookup_or_claim(
            loop=loop,
            key=key,
            inflight_key=inflight_key,
            user_id=user_id,
        )
        if hit or shared_future is None:
            return cached_value

        if not is_leader:
            return await asyncio.shield(shared_future)

        return await self._run_leader(
            key=key,
            inflight_key=inflight_key,
            user_id=user_id,
            start_gen=start_gen,
            ttl_seconds=effective_ttl,
            future=shared_future,
            loader=loader,
        )

    def invalidate_user(self, user_id: UUID) -> int:
        """Invalidate all cached entries and detach in-flight loads for the given user."""
        with self._lock:
            self._user_generations[user_id] += 1
            keys = self._user_keys.pop(user_id, set())
            for key in keys:
                self._entries.pop(key, None)

            stale_inflight = [
                inflight_key for inflight_key in self._inflight if inflight_key[1][0] == user_id
            ]
            for inflight_key in stale_inflight:
                self._inflight.pop(inflight_key, None)
            return len(keys)

    def invalidate_endpoint(self, user_id: UUID, endpoint: str) -> int:
        """Invalidate cached entries for a single endpoint belonging to user_id."""
        with self._lock:
            self._user_generations[user_id] += 1
            keys = self._user_keys.get(user_id)
            if not keys:
                return 0
            matching = [key for key in keys if key[1] == endpoint]
            for key in matching:
                self._remove_key_unlocked(key)
            stale_inflight = [
                inflight_key
                for inflight_key in self._inflight
                if inflight_key[1][0] == user_id and inflight_key[1][1] == endpoint
            ]
            for inflight_key in stale_inflight:
                self._inflight.pop(inflight_key, None)
            return len(matching)

    def clear(self) -> None:
        """Reset all entries, user indexes, generations, and in-flight tracking."""
        with self._lock:
            self._entries.clear()
            self._user_keys.clear()
            self._user_generations.clear()
            self._inflight.clear()

    def _resolve_ttl(self, ttl_seconds: float | None) -> float:
        return self._default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)

    def _lookup_or_claim(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        key: CacheKey,
        inflight_key: _InflightKey,
        user_id: UUID,
    ) -> tuple[bool, Any, asyncio.Future[Any] | None, bool, int]:
        with self._lock:
            cached = self._get_unlocked(key)
            if cached is not None:
                return True, cached, None, False, 0

            existing = self._inflight.get(inflight_key)
            if existing is not None and not existing.done():
                return False, None, existing, False, 0

            future: asyncio.Future[Any] = loop.create_future()
            future.add_done_callback(_consume_future_exception)
            self._inflight[inflight_key] = future
            return False, None, future, True, self._user_generations[user_id]

    async def _run_leader(
        self,
        *,
        key: CacheKey,
        inflight_key: _InflightKey,
        user_id: UUID,
        start_gen: int,
        ttl_seconds: float,
        future: asyncio.Future[Any],
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        try:
            result = await loader()
        except BaseException as exc:
            with self._lock:
                if self._inflight.get(inflight_key) is future:
                    self._inflight.pop(inflight_key, None)
            if not future.done():
                future.set_exception(exc)
            raise

        with self._lock:
            if self._user_generations[user_id] == start_gen:
                self._store_unlocked(key, result, ttl_seconds=ttl_seconds)
            if self._inflight.get(inflight_key) is future:
                self._inflight.pop(inflight_key, None)
        if not future.done():
            future.set_result(result)
        return result

    def _get_unlocked(self, key: CacheKey) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            self._remove_key_unlocked(key)
            return None
        return entry.value

    def _store_unlocked(self, key: CacheKey, value: Any, *, ttl_seconds: float) -> None:
        now = self._clock()
        self._entries.pop(key, None)
        self._entries[key] = _CacheEntry(value=value, expires_at=now + ttl_seconds)
        self._user_keys[key[0]].add(key)
        self._evict_if_needed_unlocked(now)

    def _remove_key_unlocked(self, key: CacheKey) -> None:
        self._entries.pop(key, None)
        user_keys = self._user_keys.get(key[0])
        if user_keys is not None:
            user_keys.discard(key)
            if not user_keys:
                self._user_keys.pop(key[0], None)

    def _evict_if_needed_unlocked(self, now: float) -> None:
        if len(self._entries) <= self._max_entries:
            return

        expired_keys = [key for key, entry in self._entries.items() if now >= entry.expires_at]
        for key in expired_keys:
            self._remove_key_unlocked(key)

        while len(self._entries) > self._max_entries:
            oldest_key = next(iter(self._entries))
            self._remove_key_unlocked(oldest_key)


analytics_cache = UserScopedTTLCache(default_ttl_seconds=30.0, max_entries=1024)
