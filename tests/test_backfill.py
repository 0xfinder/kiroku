"""Backfill: gap finding and sealing (pure), reading the archive (real ffmpeg), and a
SessionRun that lost queued audio, then is stopped mid-backfill and restarted."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import shutil
import subprocess
import sys
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
)
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath

import httpx
import pytest
from psycopg import AsyncConnection

from kiroku import archive, backfill, sessions, transcript
from kiroku.archive import ArchiveUnreadable, LengthUnknown
from kiroku.audio import AudioEvent
from kiroku.backfill import Gap, find_gaps, sealed_end
from kiroku.budget import Budget, Caps, Unlimited
from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.ledger import AudioSeconds, CostEntry, Purpose
from kiroku.sessions import Channel, Leg, claim_channels
from kiroku.source import Capture, Online, Probe, StreamlinkSource, StreamMeta, ffmpeg_command
from kiroku.transcriber import Context, Result, Transcriber, TranscriberAuthError
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import Services, SessionRun
from tests.fakes.speech import speechlike, wav

FAKE_STREAMLINK = Path(__file__).parent / "fakes" / "streamlink.py"
META = StreamMeta("broadcast-1", "fake stream", "Dota 2")
T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
NAME = "groq:whisper-large-v3-turbo"


def _services(pool: Pool, transcriber: Transcriber, data: Path) -> Services:
    return Services(pool, transcriber, data, Budget(pool, Caps()), None)


def _leg(seq: int, start: int, end: int | None) -> Leg:
    return Leg(1, seq, start, PurePosixPath(f"{seq:03d}.opus"), end)


# ---- pure --------------------------------------------------------------------------------


def test_gaps_inside_a_leg_and_before_its_end() -> None:
    legs = [_leg(1, 0, 60_000), _leg(2, 60_000, 90_000)]
    spans = [(0, 20_000), (35_000, 50_000), (60_000, 90_000)]
    assert find_gaps(legs, spans) == [
        Gap(legs[0], 20_000, 35_000),
        Gap(legs[0], 50_000, 60_000),
    ]


def test_a_leg_with_no_spans_is_one_gap_and_a_covered_leg_none() -> None:
    legs = [_leg(1, 0, 10_000), _leg(2, 10_000, 25_000)]
    assert find_gaps(legs, [(0, 10_000)]) == [Gap(legs[1], 10_000, 25_000)]


def test_an_open_leg_has_no_gaps_until_sealed() -> None:
    assert find_gaps([_leg(1, 0, None)], [(0, 5_000)]) == []


def test_a_leg_is_cut_at_the_next_legs_start() -> None:
    # Before sealing existed, a crashed leg's successor started at its last span's end.
    legs = [_leg(1, 0, 50_000), _leg(2, 30_000, 40_000)]
    assert find_gaps(legs, [(0, 20_000), (30_000, 40_000)]) == [Gap(legs[0], 20_000, 30_000)]


def test_spans_of_one_leg_never_close_another_legs_gap() -> None:
    legs = [_leg(1, 0, 10_000), _leg(2, 10_000, 20_000)]
    assert find_gaps(legs, [(0, 5_000), (12_000, 20_000)]) == [
        Gap(legs[0], 5_000, 10_000),
        Gap(legs[1], 10_000, 12_000),
    ]


def test_crashed_leg_seals_at_its_archive_length() -> None:
    leg = _leg(2, 40_000, None)
    assert sealed_end(leg, None, 31_500, [(0, 40_000), (40_000, 60_000)]) == 71_500


def test_seal_never_drops_below_committed_audio() -> None:
    # The archive lost its torn last page after that audio was already transcribed.
    leg = _leg(1, 0, None)
    assert sealed_end(leg, None, 19_400, [(0, 20_000)]) == 20_000


def test_unreadable_archive_seals_at_committed_audio() -> None:
    assert sealed_end(_leg(1, 0, None), None, None, [(0, 20_000)]) == 20_000
    assert sealed_end(_leg(1, 5_000, None), None, None, []) == 5_000


def test_seal_stops_at_the_next_legs_start() -> None:
    leg = _leg(1, 0, None)
    assert sealed_end(leg, 25_000, 60_000, [(0, 20_000), (25_000, 45_000)]) == 25_000


# ---- archive (real ffmpeg) -----------------------------------------------------------------


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, bytes]:
    """An archive written exactly as capture writes one, and the PCM capture saw."""
    path = tmp_path_factory.mktemp("archive") / "001.opus"
    pcm, _ = speechlike(30, seed=3)
    out = subprocess.run(
        ffmpeg_command("ffmpeg", path), input=wav(pcm), capture_output=True, check=True
    )
    return path, out.stdout


def _full_decode(path: Path) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
        check=True,
    ).stdout


async def _collect(path: Path, start_ms: int, end_ms: int) -> bytes:
    return b"".join([pcm async for pcm in archive.decode(path, start_ms, end_ms)])


async def test_decoded_length_matches_a_full_decode(
    recorded: tuple[Path, bytes], tmp_path: Path
) -> None:
    path, _ = recorded
    assert await archive.decoded_ms(path) == len(_full_decode(path)) // 32
    torn = tmp_path / "torn.opus"
    torn.write_bytes(path.read_bytes()[: path.stat().st_size // 2])  # a crash mid-page
    assert await archive.decoded_ms(torn) == len(_full_decode(torn)) // 32
    assert await archive.decoded_ms(tmp_path / "missing.opus") is None


async def test_decode_returns_exactly_the_range(recorded: tuple[Path, bytes]) -> None:
    path, _ = recorded
    full = _full_decode(path)
    for start_ms, end_ms in [(0, 5_000), (12_345, 19_000), (28_000, 31_000)]:
        got = await _collect(path, start_ms, end_ms)
        assert got == full[start_ms * 32 : end_ms * 32], (start_ms, end_ms)


async def test_decode_closed_early_stops_ffmpeg_promptly(recorded: tuple[Path, bytes]) -> None:
    path, _ = recorded
    decoded = archive.decode(path, 0, 30_000)
    async with asyncio.timeout(5):  # ffmpeg's unread output must not stall the close
        async for _ in decoded:
            break
        await asyncio.sleep(0.5)  # a paced consumer: ffmpeg fills the pipe meanwhile
        await decoded.aclose()


async def test_decode_of_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ArchiveUnreadable):
        await _collect(tmp_path / "missing.opus", 0, 1_000)


# ---- SessionRun: lost queued audio, then a stop mid-backfill, then a restart ----------------


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


@pytest.fixture
async def channel(database_url: str) -> AsyncGenerator[Channel]:
    async with claim_channels(database_url, "twitch", ["fake"]) as channels:
        yield channels[0]


class Groq:
    """A Groq stub that hangs while `hang` is set, and records every call it answers."""

    def __init__(self, hang: bool) -> None:
        self.hang = hang
        self.sent = 0
        self.prompts: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent += 1
        if self.hang:
            await asyncio.Event().wait()
        self.prompts.append(request.content.decode(errors="replace"))
        n = len(self.prompts)
        seg = {"no_speech_prob": 0.0, "avg_logprob": -0.1}
        return httpx.Response(
            200,
            json={"segments": [{**seg, "start": 0.5, "end": 2.0, "text": f"backfilled {n}"}]},
        )


async def _until(check: Callable[[], Awaitable[bool]], timeout_s: float = 30) -> None:
    async with asyncio.timeout(timeout_s):
        while True:
            if await check():
                return
            await asyncio.sleep(0.1)


async def test_lost_queue_is_backfilled_once_across_a_stop_and_restart(
    pool: Pool,
    channel: Channel,
    conn: AsyncConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legs 2-4 of the 2026-10-11 Gorgc run: the leg ended cleanly but its queued audio was
    never transcribed. Backfill is then interrupted by a stop and finished by a restart."""
    if shutil.which("ffprobe") is None:
        pytest.fail("ffprobe not on PATH")
    monkeypatch.setenv("FAKE_STREAM_ID", META.stream_id)
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_LEGS", "120")
    source = StreamlinkSource("fake", streamlink=(sys.executable, str(FAKE_STREAMLINK)))
    session = await sessions.open_session(pool, channel, META, T0)
    data = tmp_path / "data"

    async def leg_ended() -> bool:
        legs = await sessions.legs(pool, session)
        return bool(legs) and legs[0].audio_end_ms is not None

    async def spans() -> list[tuple[int, int]]:
        return await transcript.spans(pool, session, "groq:whisper-large-v3-turbo")

    groq = Groq(hang=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(groq)) as http:
        transcriber = GroqTranscriber(http, Secret("k"), background_interval_s=0)

        # Run 1: Groq hangs, so the whole leg is still queued when the drain gives up.
        run = SessionRun(_services(pool, transcriber, data), channel, source, session, META)
        await run.start()
        await _until(leg_ended)
        await run.stop(drain_timeout_s=0.5)
        assert await spans() == []
        groq.hang = False

        # Run 2: backfill starts (one call a second), and is stopped once one span is in.
        slow = GroqTranscriber(http, Secret("k"), background_interval_s=1)
        run = SessionRun(_services(pool, slow, data), channel, source, session, META)
        await run.start()

        async def some() -> bool:
            return len(await spans()) >= 1

        await _until(some)
        await run.stop()
        partial = await spans()
        assert partial and partial[-1][1] < 120_000, "the stop left part of the gap"

        # Run 3: a restart finishes it.
        run = SessionRun(_services(pool, transcriber, data), channel, source, session, META)
        await run.start()

        async def covered() -> bool:
            s = await spans()
            return bool(s) and s[-1][1] == 120_000

        await _until(covered)
        await run.stop()

    final = await spans()
    assert final[0][0] == 0 and final[-1][1] == 120_000
    for (_, a_end), (b_start, _) in itertools.pairwise(final):
        assert b_start == a_end, "spans tile the leg with no gap or overlap"
    assert final[: len(partial)] == partial, "a restart never redoes committed audio"

    # Exactly once: every answered call has one ledger row under the session and its one
    # utterance stored; nothing was transcribed twice.
    cur = await conn.execute(
        "SELECT count(*) FROM cost_ledger WHERE session_id = %s AND purpose = 'transcribe'",
        [session],
    )
    assert await cur.fetchone() == (len(groq.prompts),)

    # spoken_at comes from the leg, as for live utterances.
    cur = await conn.execute(
        """
        SELECT u.audio_start_ms, u.spoken_at, l.started_at FROM utterance u
        JOIN capture_leg l ON l.session_id = u.session_id AND l.seq = 1
        WHERE u.session_id = %s
        """,
        [session],
    )
    rows = await cur.fetchall()
    assert len(rows) == len(groq.prompts)
    for start_ms, spoken_at, leg_started in rows:
        assert spoken_at == leg_started + timedelta(milliseconds=start_ms)

    # Each backfilled chunk was prompted with the transcript just before it.
    assert "fake stream" in groq.prompts[0]
    assert "backfilled 1" in groq.prompts[1]
    assert await backfill.gaps(pool, session, transcriber.name) == []


# ---- failure paths -------------------------------------------------------------------------


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


async def test_a_probe_that_cannot_finish_says_nothing_about_the_file(
    recorded: tuple[Path, bytes], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = recorded
    monkeypatch.setattr(archive, "PROBE_TIMEOUT_S", 0.3)
    with pytest.raises(LengthUnknown):
        await archive.decoded_ms(path, ffprobe=_script(tmp_path, "hung", "exec sleep 30"))
    with pytest.raises(LengthUnknown):
        await archive.decoded_ms(path, ffprobe=_script(tmp_path, "oom", "kill -9 $$"))


def _write_archive(path: Path, seconds: float) -> None:
    """An archive written exactly as capture writes one."""
    pcm, _ = speechlike(seconds, seed=5)
    path.parent.mkdir(parents=True)
    subprocess.run(ffmpeg_command("ffmpeg", path), input=wav(pcm), capture_output=True, check=True)


async def _crashed_leg(pool: Pool, channel: Channel, data: Path, seconds: float) -> int:
    """A session whose only leg a crash left open after 5 s were committed."""
    session = await sessions.open_session(pool, channel, META, T0)
    leg = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg, T0)
    _write_archive(data / leg.archive_path, seconds)
    await transcript.commit(pool, session, NAME, Result(0, 5_000, (), None))
    return session


async def test_a_failed_probe_leaves_the_leg_open_for_later(
    pool: Pool, channel: Channel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _crashed_leg(pool, channel, tmp_path, 20)

    async def unknown(path: Path) -> int | None:
        raise LengthUnknown("ffprobe was killed by signal 9")

    with monkeypatch.context() as m:
        m.setattr(archive, "decoded_ms", unknown)
        with pytest.raises(LengthUnknown):
            await backfill.seal_open_legs(pool, session, NAME, tmp_path)
    assert [leg.audio_end_ms for leg in await sessions.legs(pool, session)] == [None]

    # Readable again: sealed at the archive's length, not at the 5 s committed.
    await backfill.seal_open_legs(pool, session, NAME, tmp_path)
    assert [leg.audio_end_ms for leg in await sessions.legs(pool, session)] == [20_000]


async def test_an_archive_with_nothing_decodable_seals_at_committed_audio(
    pool: Pool, channel: Channel, tmp_path: Path
) -> None:
    session = await _crashed_leg(pool, channel, tmp_path, 20)
    for leg in await sessions.legs(pool, session):
        (tmp_path / leg.archive_path).write_bytes(b"")  # crashed before the Ogg header
    await backfill.seal_open_legs(pool, session, NAME, tmp_path)
    assert [leg.audio_end_ms for leg in await sessions.legs(pool, session)] == [5_000]


async def test_one_backfill_per_session(
    pool: Pool, channel: Channel, conn: AsyncConnection, tmp_path: Path
) -> None:
    session = await _crashed_leg(pool, channel, tmp_path, 20)
    await backfill.seal_open_legs(pool, session, NAME, tmp_path)
    groq = Groq(hang=False)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(groq)) as http,
        conn.transaction(),
    ):
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", [backfill.BACKFILL_LOCK_CLASS, session]
        )
        transcriber = GroqTranscriber(http, Secret("k"), background_interval_s=0)
        assert not await backfill.run(pool, session, transcriber, tmp_path, lambda: "", Unlimited())
    assert groq.prompts == [], "the second backfill paid for nothing"


class _OneLegSource:
    """Delivers `pcm` as one leg, a second after it starts, then nothing (stream gone)."""

    platform = "twitch"
    login = "fake"

    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm

    async def probe(self) -> Probe:
        return Online(META)

    @asynccontextmanager
    async def capture(self, archive: Path) -> AsyncGenerator[Capture]:
        pcm, self._pcm = self._pcm, b""
        yield _Capture(pcm)


class _Capture:
    def __init__(self, pcm: bytes) -> None:
        self._data = pcm

    @property
    def pcm(self) -> AsyncIterator[bytes]:
        return self._read()

    async def _read(self) -> AsyncIterator[bytes]:
        if self._data:
            await asyncio.sleep(1)
            yield self._data

    async def stop_input(self) -> None:
        return None


async def test_a_cancelled_stop_does_not_leave_backfill_running(
    pool: Pool, channel: Channel, tmp_path: Path
) -> None:
    """A second SIGTERM lands while live transcription drains and a backfill call is in
    flight: the backfill must end with the stop, not outlive its run."""
    session = await _crashed_leg(pool, channel, tmp_path, 20)
    live_pcm, _ = speechlike(20, seed=9)
    groq = Groq(hang=True)  # the live chunk never comes back, so the drain waits
    before = asyncio.all_tasks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(groq)) as http:
        transcriber = GroqTranscriber(http, Secret("k"), background_interval_s=0)
        run = SessionRun(
            _services(pool, transcriber, tmp_path), channel, _OneLegSource(live_pcm), session, META
        )
        await run.start()

        async def both_in_flight() -> bool:  # backfill sent first; then the live chunk
            return groq.sent == 2

        await _until(both_in_flight)
        stopping = asyncio.create_task(run.stop(drain_timeout_s=60))
        await asyncio.sleep(0.5)
        stopping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopping
        await asyncio.sleep(0.2)
        current = asyncio.current_task()
        left = [t for t in asyncio.all_tasks() - before if t is not current and not t.done()]
        assert left == [], "tasks outlived their SessionRun"


class _AuthFailTranscriber:
    """A transcriber that refuses everything with the estimate of an earlier loss."""

    name = NAME

    def __init__(self, costs: tuple[CostEntry, ...]) -> None:
        self._costs = costs

    async def transcribe(
        self, audio: AsyncIterator[AudioEvent], context: Context
    ) -> AsyncIterator[Result]:
        # The archive read was never touched: the refusal comes before the first block.
        raise TranscriberAuthError("Groq rejected the API key (HTTP 401)", self._costs)
        yield  # unreachable: this only makes the body an async generator.


async def test_an_auth_failure_in_backfill_records_its_estimate_and_the_gap_stays(
    pool: Pool, channel: Channel, conn: AsyncConnection, tmp_path: Path
) -> None:
    session = await _crashed_leg(pool, channel, tmp_path, 20)
    await backfill.seal_open_legs(pool, session, NAME, tmp_path)
    estimate = CostEntry(
        "groq",
        "whisper-large-v3-turbo",
        Purpose.TRANSCRIBE,
        AudioSeconds(Decimal(10)),
        Decimal("0.000111"),
        0,
    )
    with pytest.raises(TranscriberAuthError):
        await backfill.run(
            pool,
            session,
            _AuthFailTranscriber((estimate,)),
            tmp_path,
            lambda: "",
            Budget(pool, Caps()).guard(session),
        )

    cur = await conn.execute(
        "SELECT count(*), sum(usd) FROM cost_ledger "
        "WHERE session_id = %s AND purpose = 'transcribe'",
        [session],
    )
    assert await cur.fetchone() == (1, Decimal("0.000111"))
    # The rejected speech is still a gap of the same shape, so a restart backfills it.
    gap = (await backfill.gaps(pool, session, NAME))[0]
    assert (gap.start_ms, gap.end_ms) == (5_000, 20_000)
