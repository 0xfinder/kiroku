"""Database retry backoff does not retry provider work or swallow cancellation."""

import asyncio

import psycopg
import pytest

from kiroku import db


async def test_retry_backoff_caps_at_thirty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    waits: list[float] = []

    async def write() -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 8:
            raise psycopg.OperationalError("offline")
        return "stored"

    async def sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(db.asyncio, "sleep", sleep)
    assert await db.retrying("test commit", write) == "stored"
    assert waits == [1, 2, 4, 8, 16, 30, 30, 30]
    assert attempts == 9


async def test_retry_propagates_cancellation() -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await db.retrying("cancelled commit", cancelled)
