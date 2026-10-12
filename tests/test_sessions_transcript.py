import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from psycopg import AsyncConnection

from kiroku import sessions, transcript
from kiroku.budget import Budget, Caps
from kiroku.config import Secret
from kiroku.db import Pool, SchemaOutOfDate, open_pool
from kiroku.ledger import AudioSeconds, CostEntry, Purpose
from kiroku.sessions import Channel, UnsealedLeg, claim_channels
from kiroku.source import Capture, Online, Probe, StreamMeta
from kiroku.transcriber import Result, Utterance
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import Services, SessionRun
from tests.fakes.speech import speechlike

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


async def test_next_leg_refuses_an_unsealed_leg(pool: Pool, channel: Channel) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    leg1 = await sessions.next_leg(pool, channel, session)
    assert (leg1.seq, leg1.audio_start_ms) == (1, 0)
    await sessions.record_leg(pool, leg1, T0)
    # Crash: leg 1 never got an end; its archive may hold more than the committed 25 s.
    await transcript.commit(pool, session, NAME, Result(0, 25_000, (), None))
    with pytest.raises(UnsealedLeg):
        await sessions.next_leg(pool, channel, session)
    assert await sessions.end_leg(pool, leg1, 31_000)  # sealed at its archive's length
    leg2 = await sessions.next_leg(pool, channel, session)
    assert (leg2.seq, leg2.audio_start_ms) == (2, 31_000)
    assert str(leg2.archive_path) == f"audio/gorgc/{session}/002.opus"


async def test_next_leg_never_starts_before_committed_audio(pool: Pool, channel: Channel) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    leg1 = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg1, T0)
    await sessions.end_leg(pool, leg1, 20_000)
    await transcript.commit(pool, session, NAME, Result(20_000, 25_000, (), None))
    assert (await sessions.next_leg(pool, channel, session)).audio_start_ms == 25_000


async def test_a_recorded_leg_end_never_moves(pool: Pool, channel: Channel) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    leg = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg, T0)
    assert await sessions.end_leg(pool, leg, 40_000)
    assert not await sessions.end_leg(pool, leg, 10_000)
    assert [x.audio_end_ms for x in await sessions.legs(pool, session)] == [40_000]


async def test_sealing_for_another_process_skips_a_watched_channel(
    database_url: str, pool: Pool
) -> None:
    """`kiroku backfill` must not seal the leg a running `kiroku watch` is capturing, and
    must not hold the watch's lock either (a watch starting meanwhile would be refused)."""
    async with claim_channels(database_url, "twitch", ["gorgc"]) as (watched,):
        session = await sessions.open_session(pool, watched, A, T0)
        leg = await sessions.next_leg(pool, watched, session)
        await sessions.record_leg(pool, leg, T0)
        assert not await sessions.end_leg(pool, leg, 9_000, unless_watched=watched)
        assert [x.audio_end_ms for x in await sessions.legs(pool, session)] == [None]
    # The watch is gone: the leg is a crashed one now.
    assert await sessions.end_leg(pool, leg, 9_000, unless_watched=watched)


class _FlakyPool:
    """Delegates to a real pool, but the first `fail` connections are lost."""

    def __init__(self, pool: Pool, fail: int) -> None:
        self._pool = pool
        self.fail = fail

    def connection(self) -> Any:
        if self.fail:
            self.fail -= 1
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return self._pool.connection()


async def test_commit_survives_a_lost_connection(
    pool: Pool, channel: Channel, conn: AsyncConnection
) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    cost = CostEntry(
        "groq", "m", Purpose.TRANSCRIBE, AudioSeconds(Decimal(20)), Decimal("0.000222"), 400
    )
    flaky = _FlakyPool(pool, fail=1)
    stored = await transcript.commit(cast(Pool, flaky), session, NAME, Result(0, 20_000, (), cost))
    assert stored and flaky.fail == 0
    cur = await conn.execute("SELECT count(*) FROM cost_ledger WHERE session_id = %s", [session])
    assert await cur.fetchone() == (1,), "the paid Result was kept, once"


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


async def test_next_leg_respects_the_runs_own_clock(pool: Pool, channel: Channel) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    leg = await sessions.next_leg(pool, channel, session, at_least_ms=52_000)
    assert leg.audio_start_ms == 52_000


async def test_logins_are_normalized_and_deduplicated(database_url: str) -> None:
    async with claim_channels(database_url, "twitch", ["Gorgc", "gorgc"]) as channels:
        assert [c.login for c in channels] == ["gorgc"]


class _DyingCapture:
    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm

    @property
    def pcm(self) -> AsyncIterator[bytes]:
        return self._read()

    async def _read(self) -> AsyncIterator[bytes]:
        yield self._pcm
        raise RuntimeError("ffmpeg crashed")

    async def stop_input(self) -> None:
        return None


class _DyingSource:
    platform = "twitch"
    login = "gorgc"

    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm

    async def probe(self) -> Probe:
        return Online(A)

    @asynccontextmanager
    async def capture(self, archive: Path) -> AsyncGenerator[Capture]:
        yield _DyingCapture(self._pcm)


async def test_stop_after_a_capture_failure_still_drains(
    pool: Pool, channel: Channel, conn: AsyncConnection, tmp_path: Path
) -> None:
    pcm, _ = speechlike(20, seed=8)
    groq = httpx.MockTransport(lambda r: httpx.Response(200, json={"segments": []}))
    async with httpx.AsyncClient(transport=groq) as http:
        session = await sessions.open_session(pool, channel, A, T0)
        services = Services(
            pool, GroqTranscriber(http, Secret("k")), tmp_path, Budget(pool, Caps()), None
        )
        run = SessionRun(services, channel, _DyingSource(pcm), session, A)
        await run.start()
        async with asyncio.timeout(10):
            while True:
                try:
                    run.raise_if_failed()
                except RuntimeError:
                    break
                await asyncio.sleep(0.05)
        await run.stop()  # must not re-raise the capture error, and must drain

    cur = await conn.execute("SELECT max(audio_end_ms) FROM transcript_span")
    assert await cur.fetchone() == (20_000,)
