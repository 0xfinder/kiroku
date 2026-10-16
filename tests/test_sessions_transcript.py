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
from kiroku.db import Pool, SchemaOutOfDate, connect, open_pool
from kiroku.ledger import AudioSeconds, CostEntry, Purpose
from kiroku.sessions import Channel, UnsealedLeg, claim_channels
from kiroku.source import Capture, Online, Probe, StreamMeta
from kiroku.transcriber import Result, TranscriberAuthError, Utterance
from kiroku.transcriber.groq import GroqTranscriber, billed
from kiroku.watch import Services, SessionRun
from tests.fakes.speech import speechlike

CHUNK_MS = 12_000  # speechlike(12) is exactly one voiced chunk (see test_groq.py)

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


async def test_channel_claim_releases_before_socket_disconnect(
    database_url: str, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    claimed: tuple[Channel, sessions.Leg] | None = None

    @asynccontextmanager
    async def delayed_disconnect(url: str) -> AsyncGenerator[AsyncConnection]:
        async with connect(url) as conn:
            yield conn
            # Keep the server connection alive through the claim's cleanup. This exposes
            # a delayed socket disconnect without depending on PostgreSQL scheduling.
            assert claimed is not None
            channel, leg = claimed
            assert await sessions.end_leg(pool, leg, 9_000, unless_watched=channel), (
                "channel claim kept its advisory lock until socket disconnect"
            )

    monkeypatch.setattr(sessions, "connect", delayed_disconnect)
    async with claim_channels(database_url, "twitch", ["gorgc"]) as (watched,):
        session = await sessions.open_session(pool, watched, A, T0)
        leg = await sessions.next_leg(pool, watched, session)
        await sessions.record_leg(pool, leg, T0)
        claimed = watched, leg
        assert not await sessions.end_leg(pool, leg, 9_000, unless_watched=watched)


@pytest.mark.parametrize("exception", [asyncio.CancelledError, sessions.ChannelBusy, ValueError])
async def test_channel_claim_keeps_original_exception_when_unlock_fails(
    database_url: str, monkeypatch: pytest.MonkeyPatch, exception: type[BaseException]
) -> None:
    held: AsyncConnection | None = None

    @asynccontextmanager
    async def broken_connection(url: str) -> AsyncGenerator[AsyncConnection]:
        nonlocal held
        async with connect(url) as conn:
            held = conn
            yield conn

    monkeypatch.setattr(sessions, "connect", broken_connection)
    original = exception("original block failure")
    with pytest.raises(exception) as caught:
        async with claim_channels(database_url, "twitch", ["gorgc"]):
            assert held is not None
            await held.close()
            raise original
    assert caught.value is original


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


class _PatientCapture:
    """Feeds one block, then waits for its stop signal: capture outlives the
    transcription failure, so the auth error is the only task error."""

    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm
        self._done = asyncio.Event()

    @property
    def pcm(self) -> AsyncIterator[bytes]:
        return self._read()

    async def _read(self) -> AsyncIterator[bytes]:
        yield self._pcm
        await self._done.wait()

    async def stop_input(self) -> None:
        self._done.set()


class _PatientSource:
    platform = "twitch"
    login = "gorgc"

    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm

    async def probe(self) -> Probe:
        return Online(A)

    @asynccontextmanager
    async def capture(self, archive: Path) -> AsyncGenerator[Capture]:
        yield _PatientCapture(self._pcm)


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


async def test_auth_after_a_lost_dispatch_records_one_estimate_and_keeps_the_speech(
    pool: Pool, channel: Channel, conn: AsyncConnection, tmp_path: Path
) -> None:
    """The live consumer records the uncertain estimate exactly once on the auth
    failure, and the chunk that was rejected stays a recoverable gap."""
    pcm, _ = speechlike(12, seed=10)
    dispatches = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal dispatches
        dispatches += 1
        if dispatches == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(401, json={"error": "invalid api key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        session = await sessions.open_session(pool, channel, A, T0)
        services = Services(
            pool,
            GroqTranscriber(http, Secret("gsk_test"), sleep=lambda _: asyncio.sleep(0)),
            tmp_path,
            Budget(pool, Caps()),
            None,
        )
        run = SessionRun(services, channel, _PatientSource(pcm), session, A)
        await run.start()

        async def any_leg() -> bool:
            return bool(await sessions.legs(pool, session))

        async with asyncio.timeout(10):
            while True:
                if await any_leg():
                    break
                await asyncio.sleep(0.05)
        # Transcription is paced by leg ends: stopping the leg flushes the buffered
        # chunk, its retry hits the auth error, and the run stops with it.
        async with asyncio.timeout(30):
            with pytest.raises(TranscriberAuthError):
                await run.stop()

    # Exactly one receipt, at the chunk's own estimate; the rejected audio is still a
    # gap for a later backfill.
    cur = await conn.execute(
        "SELECT count(*), sum(usd) FROM cost_ledger "
        "WHERE session_id = %s AND purpose = 'transcribe'",
        [session],
    )
    _, usd = billed(CHUNK_MS)
    assert await cur.fetchone() == (1, usd)
    assert await transcript.spans(pool, session, NAME) == []


async def test_transcript_commit_ids_follow_visibility_order(
    pool: Pool, channel: Channel, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await sessions.open_session(pool, channel, A, T0)
    leg = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg, T0)
    original = AsyncConnection.execute
    inserted = asyncio.Event()
    release = asyncio.Event()
    first = True

    async def paused_notify(
        self: AsyncConnection, query: Any, params: Any = None, **kwargs: Any
    ) -> Any:
        nonlocal first
        if query == "SELECT pg_notify('kiroku_transcript', %s)" and first:
            first = False
            inserted.set()
            await release.wait()
        return await original(self, query, params, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", paused_notify)
    old_id = await transcript.latest_id(pool, session)
    one = asyncio.create_task(
        transcript.commit(
            pool,
            session,
            "first",
            Result(1000, 2000, (Utterance(1000, 1900, "First commit"),), None),
        )
    )
    two: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(inserted.wait(), 1)
        two = asyncio.create_task(
            transcript.commit(
                pool,
                session,
                "second",
                Result(0, 1000, (Utterance(0, 900, "Second commit"),), None),
            )
        )
        done, _ = await asyncio.wait({two}, timeout=0.1)
        assert not done, "higher IDs became visible before an earlier session commit"
        assert await transcript.latest_id(pool, session) == old_id
        release.set()
        assert await one and await two
        cursor, affected = await transcript.changes(pool, session, old_id, 1000)
        assert cursor > old_id and affected == 0
        assert await transcript.changes(pool, session, cursor, 1000) == (cursor, None)
    finally:
        release.set()
        await asyncio.gather(one, *([two] if two else []), return_exceptions=True)
