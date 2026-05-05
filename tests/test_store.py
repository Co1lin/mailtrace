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
