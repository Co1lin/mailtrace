"""Storage layer tests. These exercise the redis interactions against fakeredis."""

from __future__ import annotations

import datetime as dt

import pytest

from mailtrace.store import Store


async def test_next_serial_increments_within_bucket(store: Store) -> None:
    today = dt.date(2024, 6, 1)
    first = await store.next_serial(today=today)
    second = await store.next_serial(today=today)
    assert second == first + 1


async def test_next_serial_uses_distinct_buckets_across_days(store: Store) -> None:
    a = await store.next_serial(today=dt.date(2024, 6, 1))
    b = await store.next_serial(today=dt.date(2024, 6, 2))
    # different bucket -> different leading digits
    assert a // 10000 != b // 10000


async def test_append_and_get_events_round_trip(store: Store) -> None:
    imb = "0040031415900000112345"
    await store.append_event(imb, {"scan_event_code": "L"})
    await store.append_event(imb, {"scan_event_code": "M"})
    events = await store.get_events(imb)
    assert [e["scan_event_code"] for e in events] == ["L", "M"]


async def test_get_events_empty_when_no_writes(store: Store) -> None:
    assert await store.get_events("missing") == []


async def test_serial_overflow_raises(store: Store) -> None:
    today = dt.date(2024, 6, 1)
    # prime the counter just below the cap
    epoch = dt.date(1970, 1, 1)
    bucket = (today - epoch).days % 50
    key = f"{Store.SERIAL_KEY_PREFIX}{bucket}"
    await store._redis.set(key, 9998)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        await store.next_serial(today=today)


# ---------------------------------------------------------------------------
# Distributed leader-election lock (used by the background poll loop so
# that only ONE uvicorn worker / replica runs the cycle at a time).
# ---------------------------------------------------------------------------


async def test_leader_lock_first_acquirer_wins(store: Store) -> None:
    key = "test:leader"
    assert await store.acquire_or_renew_leader(key, "worker-A", ttl_seconds=60) is True
    # A different worker can't take it while A holds it.
    assert await store.acquire_or_renew_leader(key, "worker-B", ttl_seconds=60) is False


async def test_leader_lock_owner_can_renew(store: Store) -> None:
    key = "test:leader"
    assert await store.acquire_or_renew_leader(key, "worker-A", ttl_seconds=60) is True
    # A can re-call to extend its own TTL — still True, no contention.
    assert await store.acquire_or_renew_leader(key, "worker-A", ttl_seconds=60) is True
    # B still locked out.
    assert await store.acquire_or_renew_leader(key, "worker-B", ttl_seconds=60) is False


async def test_leader_lock_release_lets_peer_acquire(store: Store) -> None:
    key = "test:leader"
    assert await store.acquire_or_renew_leader(key, "worker-A", ttl_seconds=60) is True
    await store.release_leader(key, "worker-A")
    # After release, B can take over.
    assert await store.acquire_or_renew_leader(key, "worker-B", ttl_seconds=60) is True
    # And A can't yank it back from B.
    assert await store.acquire_or_renew_leader(key, "worker-A", ttl_seconds=60) is False


async def test_leader_lock_release_only_if_owner(store: Store) -> None:
    """release_leader is a no-op when called by a non-owner."""
    key = "test:leader"
    await store.acquire_or_renew_leader(key, "worker-A", ttl_seconds=60)
    # B tries to release — must NOT actually delete A's lock.
    await store.release_leader(key, "worker-B")
    # A still holds it.
    assert await store.acquire_or_renew_leader(key, "worker-A", ttl_seconds=60) is True
    assert await store.acquire_or_renew_leader(key, "worker-B", ttl_seconds=60) is False


async def test_leader_lock_ttl_expiry_allows_failover(store: Store) -> None:
    """If the holder dies (no renewal), TTL expires and a peer can take it."""
    key = "test:leader"
    # Use ttl=1; we'll force expiry by deleting the key (simulating
    # what real redis would do when the second tick).
    assert await store.acquire_or_renew_leader(key, "worker-A", ttl_seconds=1) is True
    await store._redis.delete(key)  # type: ignore[attr-defined]
    # B can now grab it.
    assert await store.acquire_or_renew_leader(key, "worker-B", ttl_seconds=60) is True
