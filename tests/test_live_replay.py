"""End to end, no network: fake streamlink -> real ffmpeg -> watcher -> Groq stub -> Postgres.

Covers issue #2's acceptance criteria: replayed audio becomes stored Utterances with correct
timestamps; a stream drop reconnects to the same session without transcribing anything twice;
a restart of `kiroku watch` resumes the session; the session's cost is in the ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString, cast

import httpx
import pytest
from psycopg import AsyncConnection

from kiroku.budget import Budget, Caps
from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.gate import Gate
from kiroku.gate.jev import JevGate
from kiroku.pack import PackSelector
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


async def watch_until(
    database_url: str,
    pool: Pool,
    data_dir: Path,
    groq: GroqStub,
    done: Callable[[], Any],
    timeout_s: float = 60,
    *,
    gate: Gate | None = None,
    caps: Caps | None = None,
    select_pack: PackSelector = lambda category: None,
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
                budget=Budget(pool, caps or Caps()),
                gate=gate,
                poll_s=0.5,
                select_pack=select_pack,
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


async def query_rows(conn: AsyncConnection, query: str) -> list[tuple[Any, ...]]:
    cur = await conn.execute(cast(LiteralString, query))
    return list(await cur.fetchall())


def spans_cover(conn: AsyncConnection, end_ms: int) -> Callable[[], Any]:
    async def check() -> bool:
        rows = await query_rows(conn, "SELECT coalesce(max(audio_end_ms), 0) FROM transcript_span")
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
    await watch_until(database_url, pool, tmp_path / "data", groq, spans_cover(conn, 70_000))

    sessions = await query_rows(conn, "SELECT id, platform_stream_id, ended_at FROM stream_session")
    assert len(sessions) == 1 and sessions[0][1] == "broadcast-1"
    session_id = sessions[0][0]

    legs = await query_rows(
        conn, "SELECT seq, audio_start_ms, audio_end_ms, archive_path FROM capture_leg ORDER BY seq"
    )
    assert [(s, a, b) for s, a, b, _ in legs] == [(1, 0, 40_000), (2, 40_000, 70_000)]
    for _, start, end, path in legs:
        decoded = _decoded_seconds(tmp_path / "data" / path)
        assert decoded == pytest.approx((end - start) / 1000, abs=0.05)

    spans = await query_rows(
        conn, "SELECT audio_start_ms, audio_end_ms FROM transcript_span ORDER BY audio_start_ms"
    )
    assert spans[0][0] == 0 and spans[-1][1] == 70_000
    for (_, a_end), (b_start, _) in itertools.pairwise(spans):
        assert b_start == a_end, "spans must tile the audio with no gap or overlap"
    assert 40_000 in {end for _, end in spans}, "no span crosses the reconnect"

    utterances = await query_rows(
        conn, "SELECT audio_start_ms, spoken_at, text FROM utterance ORDER BY audio_start_ms"
    )
    assert len(utterances) == 2 * groq.calls
    assert len({text for *_, text in utterances}) == len(utterances), "nothing stored twice"

    ledger = await query_rows(
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
    await watch_until(database_url, pool, data, GroqStub(), spans_cover(conn, 40_000))
    # A second process start: same broadcast, so the same session, continuing the clock.
    await watch_until(database_url, pool, data, GroqStub(), spans_cover(conn, 70_000))

    assert len(await query_rows(conn, "SELECT id FROM stream_session")) == 1
    legs = await query_rows(conn, "SELECT audio_start_ms FROM capture_leg ORDER BY seq")
    assert [start for (start,) in legs] == sorted({start for (start,) in legs})
    spans = await query_rows(
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
    await watch_until(database_url, pool, tmp_path / "data", GroqStub(), spans_cover(conn, 15_000))

    # Stopping cuts the input; whatever ffmpeg already received is archived, recorded as
    # the leg's end, and transcribed before the run returns.
    legs = await query_rows(conn, "SELECT audio_end_ms, archive_path FROM capture_leg")
    spans = await query_rows(conn, "SELECT max(audio_end_ms) FROM transcript_span")
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


def jev_stub(request: httpx.Request) -> httpx.Response:
    questions = json.loads(request.content)["questions"]
    answers: dict[str, Any] = {
        key: {"type": "noul", "noul": 0.8} for key in questions if key.startswith("rule_")
    }
    answers["mode"] = {"choice": "chatting", "probabilities": {"chatting": 0.9}}
    return httpx.Response(
        200,
        json={"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 500}},
    )


async def test_the_gate_closes_windows_as_the_transcript_lands_and_at_session_end(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiroku import rules

    fake_stream("40,30")
    await rules.add(pool, "says_words", "Does he say words?", threshold=Decimal("0.5"))
    covered = spans_cover(conn, 70_000)

    async def ended() -> bool:
        if await covered():
            monkeypatch.delenv("FAKE_STREAM_ID", raising=False)  # the broadcast ends
        rows = await query_rows(conn, "SELECT ended_at FROM stream_session")
        return bool(rows) and rows[0][0] is not None

    async with httpx.AsyncClient(transport=httpx.MockTransport(jev_stub)) as jev:
        gate = JevGate(jev, Secret("k"))
        await watch_until(database_url, pool, tmp_path / "data", GroqStub(), ended, gate=gate)

    windows = await query_rows(
        conn, "SELECT start_ms, end_ms, status, mode FROM gate_window ORDER BY start_ms"
    )
    # A full Window while live, then the partial one when the session ended.
    assert windows == [
        (0, 45_000, "answered", "chatting"),
        (45_000, 70_000, "answered", "chatting"),
    ]
    decisions = await query_rows(conn, "SELECT probability, passed FROM gate_decision")
    assert decisions == [(Decimal("0.8000"), True)] * 2
    ledger = await query_rows(conn, "SELECT count(*) FROM cost_ledger WHERE purpose = 'gate'")
    assert ledger == [(2,)]
