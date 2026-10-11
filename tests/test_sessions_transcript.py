import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg import AsyncConnection

from kiroku import sessions, transcript
from kiroku.config import Secret
from kiroku.db import Pool, SchemaOutOfDate, open_pool
from kiroku.ledger import AudioSeconds, CostEntry, Purpose
from kiroku.sessions import Channel, claim_channels
from kiroku.source import Capture, Online, Probe, StreamMeta
from kiroku.transcriber import Result, Utterance
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import SessionRun
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
        run = SessionRun(
            pool,
            channel,
            _DyingSource(pcm),
            session,
            A,
            GroqTranscriber(http, Secret("k")),
            tmp_path,
        )
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
