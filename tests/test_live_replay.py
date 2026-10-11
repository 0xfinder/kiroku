"""End to end, no network: fake streamlink -> real ffmpeg -> watcher -> Groq stub -> Postgres.

Covers issue #2's acceptance criteria: replayed audio becomes stored Utterances with correct
timestamps; a stream drop reconnects to the same session without transcribing anything twice;
a restart of `kiroku watch` resumes the session; the session's cost is in the ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, LiteralString, cast

import httpx
import pytest
from psycopg import AsyncConnection

from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.sessions import claim_channels
from kiroku.source import StreamlinkSource
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import watch

FAKE_STREAMLINK = Path(__file__).parent / "fakes" / "streamlink.py"


@pytest.fixture
def fake_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    monkeypatch.setenv("FAKE_STREAM_ID", "broadcast-1")
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path))

    def set_legs(legs: str) -> None:
        monkeypatch.setenv("FAKE_LEGS", legs)

    return set_legs


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


class GroqStub:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(
            200,
            json={
                "segments": [
                    {
                        "start": 0.2,
                        "end": 1.0,
                        "text": f"call {self.calls} first",
                        "no_speech_prob": 0.0,
                        "avg_logprob": -0.1,
                    },
                    {
                        "start": 1.5,
                        "end": 3.0,
                        "text": f"call {self.calls} second",
                        "no_speech_prob": 0.0,
                        "avg_logprob": -0.1,
                    },
                ]
            },
        )


async def _watch_until(
    database_url: str,
    pool: Pool,
    data_dir: Path,
    groq: GroqStub,
    done: Callable[[], Any],
    timeout_s: float = 60,
) -> None:
    """Run `kiroku watch fake` until `done()` (a coroutine) returns True, then stop it."""
    async with (
        claim_channels(database_url, "twitch", ["fake"]) as channels,
        httpx.AsyncClient(transport=httpx.MockTransport(groq)) as http,
    ):
        source = StreamlinkSource("fake", streamlink=(sys.executable, str(FAKE_STREAMLINK)))
        task = asyncio.create_task(
            watch(
                pool,
                [(channels[0], source)],
                GroqTranscriber(http, Secret("k")),
                data_dir,
                poll_s=0.5,
            )
        )
        try:
            async with asyncio.timeout(timeout_s):
                while not await done():
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _rows(conn: AsyncConnection, query: str) -> list[tuple[Any, ...]]:
    cur = await conn.execute(cast(LiteralString, query))
    return list(await cur.fetchall())


def _spans_cover(conn: AsyncConnection, end_ms: int) -> Callable[[], Any]:
    async def check() -> bool:
        rows = await _rows(conn, "SELECT coalesce(max(audio_end_ms), 0) FROM transcript_span")
        return rows[0][0] >= end_ms

    return check


def _decoded_seconds(path: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True,
        check=True,
    ).stdout
    return len(out) / 32_000


async def test_stream_drop_reconnects_to_the_same_session(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
) -> None:
    fake_stream("40,30")
    groq = GroqStub()
    await _watch_until(database_url, pool, tmp_path / "data", groq, _spans_cover(conn, 70_000))

    sessions = await _rows(conn, "SELECT id, platform_stream_id, ended_at FROM stream_session")
    assert len(sessions) == 1 and sessions[0][1] == "broadcast-1"
    session_id = sessions[0][0]

    legs = await _rows(
        conn, "SELECT seq, audio_start_ms, audio_end_ms, archive_path FROM capture_leg ORDER BY seq"
    )
    assert [(s, a, b) for s, a, b, _ in legs] == [(1, 0, 40_000), (2, 40_000, 70_000)]
    for _, start, end, path in legs:
        decoded = _decoded_seconds(tmp_path / "data" / path)
        assert decoded == pytest.approx((end - start) / 1000, abs=0.05)

    spans = await _rows(
        conn, "SELECT audio_start_ms, audio_end_ms FROM transcript_span ORDER BY audio_start_ms"
    )
    assert spans[0][0] == 0 and spans[-1][1] == 70_000
    for (_, a_end), (b_start, _) in itertools.pairwise(spans):
        assert b_start == a_end, "spans must tile the audio with no gap or overlap"
    assert 40_000 in {end for _, end in spans}, "no span crosses the reconnect"

    utterances = await _rows(
        conn, "SELECT audio_start_ms, spoken_at, text FROM utterance ORDER BY audio_start_ms"
    )
    assert len(utterances) == 2 * groq.calls
    assert len({text for *_, text in utterances}) == len(utterances), "nothing stored twice"

    ledger = await _rows(
        conn,
        f"SELECT count(*), sum(audio_seconds) FROM cost_ledger WHERE session_id = {session_id}",
    )
    assert ledger[0][0] == groq.calls
    assert ledger[0][1] >= 70


async def test_restarting_watch_resumes_the_session(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
) -> None:
    fake_stream("40,30")
    data = tmp_path / "data"
    await _watch_until(database_url, pool, data, GroqStub(), _spans_cover(conn, 40_000))
    # A second process start: same broadcast, so the same session, continuing the clock.
    await _watch_until(database_url, pool, data, GroqStub(), _spans_cover(conn, 70_000))

    assert len(await _rows(conn, "SELECT id FROM stream_session")) == 1
    legs = await _rows(conn, "SELECT audio_start_ms FROM capture_leg ORDER BY seq")
    assert [start for (start,) in legs] == sorted({start for (start,) in legs})
    spans = await _rows(
        conn, "SELECT audio_start_ms, audio_end_ms FROM transcript_span ORDER BY audio_start_ms"
    )
    for (_, a_end), (b_start, _) in itertools.pairwise(spans):
        assert b_start >= a_end, "a restart never re-transcribes committed audio"


async def test_stopping_mid_leg_transcribes_everything_captured(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_stream("37")
    monkeypatch.setenv("FAKE_HOLD", "1")  # the leg is still live when watch stops
    await _watch_until(
        database_url, pool, tmp_path / "data", GroqStub(), _spans_cover(conn, 15_000)
    )

    # Stopping cuts the input; whatever ffmpeg already received is archived, recorded as
    # the leg's end, and transcribed before the run returns.
    legs = await _rows(conn, "SELECT audio_end_ms, archive_path FROM capture_leg")
    spans = await _rows(conn, "SELECT max(audio_end_ms) FROM transcript_span")
    assert len(legs) == 1
    leg_end, path = legs[0]
    assert leg_end is not None and leg_end > 15_000
    assert spans[0][0] == leg_end, "the drain on shutdown commits the tail of the leg"
    assert _decoded_seconds(tmp_path / "data" / path) == pytest.approx(leg_end / 1000, abs=0.05)


async def test_second_watcher_on_a_channel_is_refused(database_url: str) -> None:
    from kiroku.sessions import ChannelBusy

    async with claim_channels(database_url, "twitch", ["fake"]):
        with pytest.raises(ChannelBusy):
            async with claim_channels(database_url, "twitch", ["fake"]):
                pass
