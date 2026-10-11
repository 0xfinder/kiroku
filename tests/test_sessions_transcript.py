from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import AsyncConnection

from kiroku import sessions, transcript
from kiroku.db import Pool, SchemaOutOfDate, open_pool
from kiroku.ledger import AudioSeconds, CostEntry, Purpose
from kiroku.sessions import Channel, claim_channels
from kiroku.source import StreamMeta
from kiroku.transcriber import Result, Utterance

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
A = StreamMeta("broadcast-a", "title", "Dota 2")
B = StreamMeta("broadcast-b", "title", "Dota 2")
NAME = "groq:whisper-large-v3-turbo"


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


@pytest.fixture
async def channel(database_url: str) -> AsyncGenerator[Channel]:
    async with claim_channels(database_url, "twitch", ["gorgc"]) as channels:
        yield channels[0]


async def test_same_broadcast_is_one_session_even_after_a_close(
    pool: Pool, channel: Channel
) -> None:
    first = await sessions.open_session(pool, channel, A, T0)
    assert await sessions.open_session(pool, channel, A, T0) == first
    await sessions.close_open_sessions(pool, channel, T0 + timedelta(minutes=2))
    assert await sessions.open_session(pool, channel, A, T0 + timedelta(minutes=3)) == first


async def test_new_broadcast_closes_the_stale_session(
    pool: Pool, channel: Channel, conn: AsyncConnection
) -> None:
    first = await sessions.open_session(pool, channel, A, T0)
    second = await sessions.open_session(pool, channel, B, T0 + timedelta(hours=8))
    assert second != first
    cur = await conn.execute("SELECT id FROM stream_session WHERE ended_at IS NULL")
    assert await cur.fetchall() == [(second,)]


async def test_next_leg_never_starts_before_committed_audio(pool: Pool, channel: Channel) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    leg1 = await sessions.next_leg(pool, channel, session)
    assert (leg1.seq, leg1.audio_start_ms) == (1, 0)
    await sessions.record_leg(pool, leg1, T0)
    # Crash: leg 1 never got an end, but a span up to 25 s was committed.
    await transcript.commit(pool, session, NAME, Result(0, 25_000, (), None))
    leg2 = await sessions.next_leg(pool, channel, session)
    assert (leg2.seq, leg2.audio_start_ms) == (2, 25_000)
    assert str(leg2.archive_path) == f"audio/gorgc/{session}/002.opus"


async def test_commit_is_idempotent_and_derives_wall_clock_time(
    pool: Pool, channel: Channel, conn: AsyncConnection
) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    leg1 = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg1, T0)
    await sessions.end_leg(pool, leg1, 40_000)
    leg2 = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg2, T0 + timedelta(minutes=5))  # 260 s wall-clock gap

    cost = CostEntry(
        "groq", "m", Purpose.TRANSCRIBE, AudioSeconds(Decimal(20)), Decimal("0.000222"), 400
    )
    result = Result(40_000, 60_000, (Utterance(41_000, 43_000, "after the gap"),), cost)
    assert await transcript.commit(pool, session, NAME, result) is True
    assert await transcript.commit(pool, session, NAME, result) is False

    cur = await conn.execute("SELECT spoken_at, text FROM utterance")
    assert await cur.fetchall() == [(T0 + timedelta(minutes=5, seconds=1), "after the gap")]
    cur = await conn.execute(
        "SELECT count(*), sum(usd) FROM cost_ledger WHERE session_id = %s", [session]
    )
    assert await cur.fetchone() == (1, Decimal("0.000222"))


async def test_utterance_without_a_capture_leg_is_refused(pool: Pool, channel: Channel) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    result = Result(0, 10_000, (Utterance(0, 1_000, "no anchor"),), None)
    with pytest.raises(psycopg.errors.NotNullViolation):
        await transcript.commit(pool, session, NAME, result)


async def test_pool_refuses_a_stale_schema(blank_database_url: str) -> None:
    with pytest.raises(SchemaOutOfDate, match="kiroku db migrate"):
        async with open_pool(blank_database_url):
            pass
