"""Unit tests for the thread-safe and async-safe UserScopedTTLCache."""

import asyncio
import threading
from uuid import uuid4

import pytest

from app.core.cache import UserScopedTTLCache, build_cache_key, normalize_cache_params


def test_cache_key_normalization_is_deterministic() -> None:
    """Canonicalize query params regardless of key order, whitespace, booleans, or None."""
    user_id = uuid4()
    game_id = uuid4()

    key_a = build_cache_key(
        user_id,
        "analytics:clips",
        {"period": "30d", "q": "  clutch  ", "game_id": game_id, "privacy": None, "mine": True},
    )
    key_b = build_cache_key(
        user_id,
        "analytics:clips",
        {"mine": True, "privacy": None, "game_id": game_id, "q": "clutch", "period": "30d"},
    )
    assert key_a == key_b
    assert normalize_cache_params(None) == ()
    assert normalize_cache_params({"flag": False}) == (("flag", "false"),)


def test_ttl_expiration_user_and_endpoint_invalidation_and_eviction() -> None:
    """Verify TTL expiration, per-user/endpoint invalidation, and bounded capacity eviction."""
    now = 100.0
    cache = UserScopedTTLCache(default_ttl_seconds=30.0, max_entries=3, clock=lambda: now)
    user_a = uuid4()
    user_b = uuid4()

    assert cache.default_ttl_seconds == 30.0
    assert cache.get(user_a, "analytics:overview", {"period": "30d"}) is None

    # TTL <= 0 bypasses storage
    cache.set(user_a, "analytics:overview", {"v": 0}, params={"period": "30d"}, ttl_seconds=0)
    assert cache.get(user_a, "analytics:overview", {"period": "30d"}) is None

    # Populate entries
    cache.set(user_a, "analytics:overview", {"v": 1}, params={"period": "30d"})
    cache.set(user_a, "analytics:clips", {"v": 2}, params={"page": 1})
    cache.set(user_b, "analytics:overview", {"v": 3}, params={"period": "30d"})
    assert cache.get(user_a, "analytics:overview", {"period": "30d"}) == {"v": 1}

    # Bounded capacity evicts oldest entry when full
    cache.set(user_b, "analytics:clips", {"v": 4}, params={"page": 1})
    assert cache.get(user_a, "analytics:overview", {"period": "30d"}) is None
    assert cache.get(user_a, "analytics:clips", {"page": 1}) == {"v": 2}
    assert cache.get(user_b, "analytics:clips", {"page": 1}) == {"v": 4}

    # Endpoint invalidation only clears matching endpoint for target user
    removed_ep = cache.invalidate_endpoint(user_b, "analytics:clips")
    assert removed_ep == 1
    assert cache.get(user_b, "analytics:clips", {"page": 1}) is None
    assert cache.get(user_b, "analytics:overview", {"period": "30d"}) == {"v": 3}
    assert cache.invalidate_endpoint(uuid4(), "analytics:clips") == 0

    # User invalidation clears all entries for user_a without affecting user_b
    cache.set(user_a, "analytics:overview", {"v": 5}, params={"period": "7d"})
    removed_user = cache.invalidate_user(user_a)
    assert removed_user == 2
    assert cache.get(user_a, "analytics:clips", {"page": 1}) is None
    assert cache.get(user_b, "analytics:overview", {"period": "30d"}) == {"v": 3}

    # TTL expiration lazily evicts expired entry
    now = 131.0
    assert cache.get(user_b, "analytics:overview", {"period": "30d"}) is None

    # Expired entries are purged before evicting live ones when capacity is exceeded
    cache.set(user_a, "ep1", 1, ttl_seconds=5.0)
    cache.set(user_a, "ep2", 2, ttl_seconds=50.0)
    cache.set(user_a, "ep3", 3, ttl_seconds=50.0)
    now = 140.0
    cache.set(user_a, "ep4", 4, ttl_seconds=50.0)
    assert cache.get(user_a, "ep1") is None
    assert cache.get(user_a, "ep2") == 2
    assert cache.get(user_a, "ep4") == 4

    cache.clear()
    assert cache.get(user_a, "ep2") is None


def test_cache_thread_safety_under_concurrent_access() -> None:
    """Confirm concurrent thread reads, writes, and invalidations raise no errors."""
    cache = UserScopedTTLCache(default_ttl_seconds=30.0, max_entries=64)
    users = [uuid4() for _ in range(4)]
    errors: list[Exception] = []

    def _worker(worker_idx: int) -> None:
        try:
            for step in range(40):
                uid = users[(worker_idx + step) % len(users)]
                cache.set(uid, "analytics:overview", step, params={"period": "30d"})
                cache.get(uid, "analytics:overview", {"period": "30d"})
                if step % 10 == 0:
                    cache.invalidate_user(uid)
        except Exception as exc:  # pragma: no cover - assertion guard
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(idx,)) for idx in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


async def test_get_or_set_coalesces_inflight_handles_errors_and_respects_invalidation() -> None:
    """Coalesce concurrent loads, propagate errors safely, and avoid caching invalidated loads."""
    now = 200.0
    cache = UserScopedTTLCache(default_ttl_seconds=30.0, clock=lambda: now)
    user_id = uuid4()

    # 1. Zero TTL bypasses cache
    direct_calls = 0

    async def _direct_loader() -> str:
        nonlocal direct_calls
        direct_calls += 1
        return f"direct-{direct_calls}"

    assert await cache.get_or_set(user_id, "ep", _direct_loader, ttl_seconds=0) == "direct-1"
    assert await cache.get_or_set(user_id, "ep", _direct_loader, ttl_seconds=0) == "direct-2"

    # 2. Concurrent callers coalesce into a single in-flight loader execution
    gate = asyncio.Event()
    load_count = 0

    async def _slow_loader() -> dict[str, int]:
        nonlocal load_count
        load_count += 1
        await gate.wait()
        return {"runs": load_count}

    tasks = [
        asyncio.create_task(
            cache.get_or_set(user_id, "analytics:overview", _slow_loader, params={"period": "30d"})
        )
        for _ in range(5)
    ]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert load_count == 1
    assert all(res == {"runs": 1} for res in results)

    # Subsequent call hits cache without invoking loader
    cached = await cache.get_or_set(
        user_id,
        "analytics:overview",
        _slow_loader,
        params={"period": "30d"},
    )
    assert cached == {"runs": 1}
    assert load_count == 1

    # 3. Invalidation during an in-flight load prevents stale value from being cached
    cache.invalidate_user(user_id)
    started = asyncio.Event()
    finish = asyncio.Event()
    version = 0

    async def _racy_loader() -> str:
        nonlocal version
        version += 1
        current = version
        started.set()
        await finish.wait()
        return f"v{current}"

    in_flight_task = asyncio.create_task(
        cache.get_or_set(user_id, "analytics:clips", _racy_loader, params={"page": 1})
    )
    await started.wait()
    cache.invalidate_user(user_id)
    finish.set()
    assert await in_flight_task == "v1"
    assert cache.get(user_id, "analytics:clips", {"page": 1}) is None

    # 4. Exceptions propagate to all waiters and clean up in-flight state
    fail_gate = asyncio.Event()

    async def _failing_loader() -> str:
        await fail_gate.wait()
        msg = "boom"
        raise RuntimeError(msg)

    fail_tasks = [
        asyncio.create_task(cache.get_or_set(user_id, "analytics:fail", _failing_loader))
        for _ in range(2)
    ]
    await asyncio.sleep(0)
    fail_gate.set()
    for task in fail_tasks:
        with pytest.raises(RuntimeError, match="boom"):
            await task

    # Retry succeeds cleanly after failure cleanup
    async def _ok_loader() -> str:
        return "recovered"

    assert await cache.get_or_set(user_id, "analytics:fail", _ok_loader) == "recovered"
