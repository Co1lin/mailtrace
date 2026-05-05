"""Redis-backed storage: serial counter and pushed IV scan events."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Protocol

from redis.asyncio import Redis


class SerialAllocator(Protocol):
    async def next_serial(self) -> int: ...


class Store:
    """Thin wrapper around the redis async client used by the app.

    Encapsulates all key naming so callers cannot drift.
    """

    SERIAL_KEY_PREFIX = "mailtrace:serial:"
    IMB_EVENTS_PREFIX = "mailtrace:imb:"

    def __init__(
        self,
        redis: Redis[Any],
        *,
        rolling_window_days: int,
        event_ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._rolling_window_days = rolling_window_days
        self._event_ttl_seconds = event_ttl_seconds

    @classmethod
    def from_url(cls, url: str, *, rolling_window_days: int, event_ttl_seconds: int) -> Store:
        return cls(
            Redis.from_url(url, decode_responses=False),
            rolling_window_days=rolling_window_days,
            event_ttl_seconds=event_ttl_seconds,
        )

    async def close(self) -> None:
        # redis-py 5+: aclose. Older type stubs only know about close().
        aclose = getattr(self._redis, "aclose", None)
        if aclose is not None:
            await aclose()
        else:
            await self._redis.close()

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def next_serial(self, *, today: dt.date | None = None) -> int:
        """Allocate a 6-digit serial that is unique within a 50-day window.

        The serial is `(day_bucket * 10000) + per_day_counter`, capped at 9999
        per day. The bucket key expires 48h after the most recent increment,
        so unused serials do not pile up. The counter is reused after the
        rolling window wraps, which is well outside USPS's typical scan
        delivery window.
        """
        today = today or dt.date.today()
        epoch = dt.date(1970, 1, 1)
        bucket = (today - epoch).days % self._rolling_window_days
        key = f"{self.SERIAL_KEY_PREFIX}{bucket}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 48 * 60 * 60)
            counter, _ = await pipe.execute()
        if counter >= 9999:
            raise RuntimeError("daily serial bucket exhausted")
        return bucket * 10000 + int(counter)

    @classmethod
    def _imb_key(cls, imb: str) -> str:
        return f"{cls.IMB_EVENTS_PREFIX}{imb}"

    async def append_event(self, imb: str, event: dict[str, Any]) -> None:
        key = self._imb_key(imb)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, json.dumps(event, separators=(",", ":")))
            pipe.expire(key, self._event_ttl_seconds)
            await pipe.execute()

    async def get_events(self, imb: str) -> list[dict[str, Any]]:
        raw = await self._redis.lrange(self._imb_key(imb), 0, -1)
        return [json.loads(item) for item in raw]

    # Token persistence used by the USPS client.

    async def get_str(self, key: str) -> str | None:
        value = await self._redis.get(key)
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def set_str(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        if ttl_seconds is not None:
            await self._redis.set(key, value, ex=ttl_seconds)
        else:
            await self._redis.set(key, value)
