import itertools
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest

from kiroku.audio import AudioBlock, AudioEvent, LegEnded
from kiroku.budget import OverBudget, Refusal, Unlimited
from kiroku.config import Secret
from kiroku.ledger import AudioSeconds
from kiroku.transcriber import Context, Result
from kiroku.transcriber.groq import (
    GroqAuthError,
    GroqTranscriber,
    billed,
    retry_delay,
    to_utterances,
)
from tests.fakes.speech import speechlike


def _segments(*segs: tuple[float, float, str]) -> dict[str, Any]:
    return {
        "text": " ".join(t for _, _, t in segs),
        "segments": [
            {"start": s, "end": e, "text": t, "no_speech_prob": 0.01, "avg_logprob": -0.2}
            for s, e, t in segs
        ],
    }


class FakeGroq:
    """httpx transport that answers like Groq and records what it was sent."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = responses or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(200, json=_segments((0.5, 2.0, f"words {len(self.requests)}")))


def _without_boundary(request: httpx.Request) -> bytes:
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    return request.content.replace(boundary, b"")


def _transcriber(fake: FakeGroq, sleeps: list[float] | None = None) -> GroqTranscriber:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return GroqTranscriber(http, Secret("gsk_test"), sleep=sleep)


async def _events(*events: AudioEvent) -> AsyncIterator[AudioEvent]:
    for e in events:
        yield e


async def _collect(t: GroqTranscriber, *events: AudioEvent) -> list[Result]:
    ctx = Context(lambda: "Dota 2 stream.", Unlimited())
    return [r async for r in t.transcribe(_events(*events), ctx)]


async def test_results_tile_the_audio_and_map_to_the_session_clock() -> None:
    fake = FakeGroq()
    pcm, _ = speechlike(60, seed=1)
    results = await _collect(_transcriber(fake), AudioBlock(16_000 * 100, pcm), LegEnded())

    assert results[0].start_ms == 100_000
    assert results[-1].end_ms == 160_000
    for a, b in itertools.pairwise(results):
        assert b.start_ms == a.end_ms
    first = results[0].utterances[0]
    assert (first.start_ms, first.end_ms) == (100_500, 102_000)
    assert len(fake.requests) == len(results)
    assert all(r.cost is not None and r.cost.provider == "groq" for r in results)


async def test_prompt_carries_vocabulary_and_previous_text() -> None:
    fake = FakeGroq()
    pcm, _ = speechlike(45, seed=2)
    await _collect(_transcriber(fake), AudioBlock(0, pcm), LegEnded())
    second = fake.requests[1].content.decode(errors="replace")
    assert "Dota 2 stream. words 1" in second


async def test_silent_chunks_commit_a_span_without_a_paid_call() -> None:
    fake = FakeGroq()
    results = await _collect(_transcriber(fake), AudioBlock(0, bytes(16_000 * 2 * 30)))
    assert fake.requests == []
    assert results and all(r.cost is None and not r.utterances for r in results)
    assert results[-1].end_ms == 30_000


async def test_rate_limit_waits_and_resends_the_same_audio() -> None:
    limited = httpx.Response(429, headers={"retry-after": "7"})
    fake = FakeGroq([limited])
    sleeps: list[float] = []
    pcm, _ = speechlike(12, seed=3)
    results = await _collect(_transcriber(fake, sleeps), AudioBlock(0, pcm))

    assert len(fake.requests) == 2
    assert _without_boundary(fake.requests[0]) == _without_boundary(fake.requests[1])
    assert sleeps and 6.0 < sleeps[0] <= 7.0
    assert len(results) == 1 and results[0].cost is not None


async def test_bad_key_stops_with_an_auth_error() -> None:
    fake = FakeGroq([httpx.Response(401, json={"error": "invalid api key"})])
    pcm, _ = speechlike(12, seed=4)
    with pytest.raises(GroqAuthError):
        await _collect(_transcriber(fake), AudioBlock(0, pcm))


async def test_rejected_audio_commits_an_empty_unbilled_span() -> None:
    fake = FakeGroq([httpx.Response(400, json={"error": "could not decode audio"})])
    pcm, _ = speechlike(12, seed=5)
    results = await _collect(_transcriber(fake), AudioBlock(0, pcm))
    assert len(results) == 1
    assert results[0].utterances == () and results[0].cost is None


def test_billing_has_a_ten_second_minimum() -> None:
    assert billed(4_000) == (Decimal(10), Decimal("0.000111"))
    assert billed(25_000)[0] == Decimal(25)


def test_utterance_starts_are_strictly_increasing_and_inside_the_chunk() -> None:
    body = _segments((0.0, 1.0, "a"), (0.0, 1.0, "b"), (30.0, 31.0, "c"), (1.0, 2.0, " "))
    utterances, text = to_utterances(10_000, 20_000, body)
    starts = [u.start_ms for u in utterances]
    assert starts == sorted(set(starts))
    assert all(10_000 <= u.start_ms < u.end_ms <= 20_000 for u in utterances)
    assert text == "a b c"


def test_likely_non_speech_segments_are_dropped() -> None:
    body = {
        "segments": [
            {"start": 0, "end": 1, "text": "Thank you.", "no_speech_prob": 0.9, "avg_logprob": -1.5}
        ]
    }
    assert to_utterances(0, 10_000, body) == ((), "")


@pytest.mark.parametrize(
    ("status", "headers", "attempt", "expected"),
    [
        (429, {"retry-after": "3"}, 1, 3.0),
        (429, {"x-ratelimit-reset-audio-seconds": "2m59.5s"}, 1, 179.5),
        (429, {"x-ratelimit-reset-requests": "750ms"}, 1, 0.75),
        (503, {}, 3, 4.0),
        (None, {}, 10, 60.0),
    ],
)
def test_retry_delay(
    status: int | None, headers: dict[str, str], attempt: int, expected: float
) -> None:
    assert retry_delay(status, headers, attempt) == pytest.approx(expected)


async def test_a_failing_audio_source_raises_instead_of_hanging() -> None:
    import asyncio

    async def broken() -> AsyncIterator[AudioEvent]:
        pcm, _ = speechlike(5, seed=6)
        yield AudioBlock(0, pcm)
        raise RuntimeError("capture died")

    t = _transcriber(FakeGroq())
    with pytest.raises(RuntimeError, match="capture died"):
        async with asyncio.timeout(10):
            async for _ in t.transcribe(broken(), Context(lambda: "", Unlimited())):
                pass


async def test_a_lost_response_is_billed_rather_than_dropped() -> None:
    fake = FakeGroq()
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return fake(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    t = GroqTranscriber(http, Secret("k"), sleep=lambda _: asyncio_sleep0())
    pcm, _ = speechlike(12, seed=7)
    events = _events(AudioBlock(0, pcm))
    results = [r async for r in t.transcribe(events, Context(lambda: "", Unlimited()))]
    cost = results[0].cost
    assert cost is not None and cost.usage == AudioSeconds(Decimal(24))  # 2 x 12 s


async def asyncio_sleep0() -> None:
    return None


def test_segments_without_room_merge_into_the_previous_utterance() -> None:
    body = _segments((10.0, 10.5, "late one"), (10.2, 10.5, "late two"))
    utterances, text = to_utterances(0, 10_000, body)
    assert [u.text for u in utterances] == ["late one late two"]
    assert text == "late one late two"


async def test_a_refused_chunk_is_left_as_a_gap_and_nothing_is_sent() -> None:
    class RefuseFirst:
        def __init__(self) -> None:
            self.asked: list[Decimal] = []

        async def check(self, estimate_usd: Decimal) -> None:
            self.asked.append(estimate_usd)
            if len(self.asked) == 1:
                raise OverBudget(Refusal("month", Decimal(20), Decimal(20), estimate_usd))

    fake = FakeGroq()
    guard = RefuseFirst()
    pcm, _ = speechlike(40, seed=9)
    t = _transcriber(fake)
    events = _events(AudioBlock(0, pcm), LegEnded())
    results = [r async for r in t.transcribe(events, Context(lambda: "", guard))]
    # Three voiced chunks: the first refused (no Result, no request), the others sent.
    assert len(guard.asked) == 3 and len(fake.requests) == 2
    assert len(results) == 2 and results[0].start_ms > 0
    assert all(r.cost is not None for r in results)
