"""Terminal provider exits preserve uncertain dispatch costs (P1).

A dispatch that was sent and lost (a transport error that could have been billed: any
httpx failure except a failed connect) carries a conservative estimate while it is
being retried. The matrix pins, per voice-capable provider, what each terminal exit
does with it: a direct refusal bills nothing, a terminal non-auth 4xx after a lost
dispatch keeps one estimate, a terminal auth keeps one estimate on its exception, and
a retry then success bills the same valid total. The Groq JSON provider is the
control; it already behaves this way and must keep doing so.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest

from kiroku.audio import AudioBlock, AudioEvent, LegEnded
from kiroku.budget import Unlimited
from kiroku.config import Secret
from kiroku.gate import GateAuthError, GateBadResponse, GateRequest, rule_question
from kiroku.gate.jev import (
    DEFAULT_MODEL as JEV_MODEL,
)
from kiroku.gate.jev import (
    JevGate,
)
from kiroku.gate.jev import (
    estimate_tokens as jev_estimate,
)
from kiroku.gate.jev import (
    request_body as jev_body,
)
from kiroku.gate.jev import (
    usd as jev_usd,
)
from kiroku.gate.windows import GateState
from kiroku.ledger import AudioSeconds, CostEntry, Purpose, Tokens
from kiroku.llm import LLMAuthError, LLMBadResponse, LLMRequest
from kiroku.llm.groq import MODEL as LLM_MODEL
from kiroku.llm.groq import PRICE, GroqLLM, estimate_tokens, request_body
from kiroku.rules import Rule
from kiroku.transcriber import Context, Result, TranscriberAuthError
from kiroku.transcriber.groq import GroqTranscriber, billed
from tests.fakes.speech import speechlike
from tests.test_gate import Clock, FakeJev

CHUNK_MS = 12_000  # speechlike(12) is exactly one voiced chunk (see test_groq.py)


async def _sleep0(_seconds: float) -> None:
    return None


async def _events(*items: AudioEvent) -> AsyncIterator[AudioEvent]:
    for item in items:
        yield item


# ---- Jev (the Gate adapter): terminal exits --------------------------------------------------


_RULE = Rule(7, "predicts_winner", "Does he predict the winner?", Decimal("0.60"), None, 600)


def _gate_request() -> GateRequest:
    # The same state and one rule question; the estimate is over its JSON body.
    return GateRequest(
        GateState({"stream": {"title": "t"}}, "I think Spirit wins this."),
        (rule_question(_RULE),),
        Unlimited(),
    )


def _jev_lost_cost() -> CostEntry:
    tokens = jev_estimate(jev_body(JEV_MODEL, _gate_request()))
    return CostEntry("jev", JEV_MODEL, Purpose.GATE, Tokens(tokens), jev_usd(tokens), 0)


async def _jev(fake: FakeJev) -> Any:
    clock = Clock()
    gate = JevGate(
        httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        Secret("k"),
        sleep=clock.sleep,
        clock=clock,
    )
    return await gate.evaluate(_gate_request())


@pytest.mark.parametrize(
    ("first_response", "expected"),
    [
        (httpx.Response(400, text="bad question"), GateBadResponse),
        (httpx.Response(401, json={}), GateAuthError),
    ],
)
async def test_jev_a_direct_refusal_bills_nothing(
    first_response: httpx.Response, expected: type[Any]
) -> None:
    fake = FakeJev(first_response)
    with pytest.raises(expected) as e:
        await _jev(fake)
    assert (len(fake.requests), e.value.costs) == (1, ())


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        (httpx.Response(400, text="bad question"), GateBadResponse),
        (httpx.Response(401, json={}), GateAuthError),
    ],
)
async def test_jev_a_lost_dispatch_keeps_one_estimate(
    terminal: httpx.Response, expected: type[Any]
) -> None:
    fake = FakeJev(httpx.ReadTimeout("lost", request=None), terminal)
    with pytest.raises(expected) as e:
        await _jev(fake)
    assert len(fake.requests) == 2
    assert e.value.costs == (_jev_lost_cost(),)


async def test_jev_a_retry_then_success_keeps_the_valid_total() -> None:
    body = {
        "model": JEV_MODEL,
        "answers": {
            "mode": {
                "choice": "chatting",
                "confidence": 0.8,
                "probabilities": {"playing": 0.05, "watching_pro": 0.05, "chatting": 0.9},
            },
            "rule_7": {"type": "noul", "noul": 0.9},
        },
        "usage": {"input_tokens": 1000, "output_tokens": 40},
    }
    fake = FakeJev(httpx.ReadTimeout("lost", request=None), httpx.Response(200, json=body))
    answer = await _jev(fake)
    assert len(fake.requests) == 2
    # The answered call's usage, doubled for the lost dispatch of the same size.
    assert answer.costs[0].usage == Tokens(2000, 80)
    assert answer.costs[0].usd == jev_usd(2000)


# ---- Whisper (the transcriber): terminal exits ------------------------------------------------


class FakeGroq:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _whisper_ok() -> dict[str, Any]:
    return {
        "text": "words",
        "segments": [
            {"start": 0.5, "end": 2.0, "text": "words", "no_speech_prob": 0.01, "avg_logprob": -0.2}
        ],
    }


async def _whisper(fake: FakeGroq) -> list[Result]:
    pcm, _ = speechlike(12, seed=21)
    t = GroqTranscriber(
        httpx.AsyncClient(transport=httpx.MockTransport(fake)), Secret("gsk_test"), sleep=_sleep0
    )
    return [
        r
        async for r in t.transcribe(
            _events(AudioBlock(0, pcm), LegEnded()), Context(lambda: "", Unlimited())
        )
    ]


def _whisper_lost(n: int) -> CostEntry:
    seconds, usd = billed(CHUNK_MS)
    return CostEntry(
        "groq", "whisper-large-v3-turbo", Purpose.TRANSCRIBE, AudioSeconds(seconds * n), usd * n, 0
    )


async def test_whisper_a_direct_400_bills_nothing() -> None:
    fake = FakeGroq(httpx.Response(400, json={"error": "bad audio"}))
    results = await _whisper(fake)
    assert (len(fake.requests), len(results)) == (1, 1)
    assert results[0].utterances == () and results[0].cost is None


async def test_whisper_a_direct_401_bills_nothing() -> None:
    fake = FakeGroq(httpx.Response(401, json={"error": "invalid api key"}))
    with pytest.raises(TranscriberAuthError) as e:
        await _whisper(fake)
    assert (len(fake.requests), e.value.costs) == (1, ())


async def test_whisper_a_lost_dispatch_keeps_one_estimate_on_400() -> None:
    fake = FakeGroq(
        httpx.ReadTimeout("lost", request=None),
        httpx.Response(400, json={"error": "bad audio"}),
    )
    results = await _whisper(fake)
    assert len(fake.requests) == 2
    assert len(results) == 1 and results[0].utterances == ()
    assert results[0].cost == _whisper_lost(1)


async def test_whisper_a_lost_dispatch_keeps_one_estimate_on_auth() -> None:
    fake = FakeGroq(
        httpx.ReadTimeout("lost", request=None),
        httpx.Response(401, json={"error": "invalid api key"}),
    )
    with pytest.raises(TranscriberAuthError) as e:
        await _whisper(fake)
    assert len(fake.requests) == 2
    assert e.value.costs == (_whisper_lost(1),)


async def test_whisper_a_retry_then_success_keeps_the_valid_total() -> None:
    fake = FakeGroq(
        httpx.ReadTimeout("lost", request=None), httpx.Response(200, json=_whisper_ok())
    )
    results = await _whisper(fake)
    assert len(fake.requests) == 2
    # The answered call's billing, doubled for the lost dispatch of the same size: the
    # per-call price times two, not a larger chunk billed once.
    seconds, usd = billed(CHUNK_MS)
    cost = results[0].cost
    assert cost is not None and cost.usage == AudioSeconds(seconds * 2) and cost.usd == usd * 2


# ---- Groq JSON (the control): terminal exits --------------------------------------------------


def _llm_request() -> LLMRequest:
    return LLMRequest("s", "{}", Purpose.EXTRACTION, Unlimited(), 10, 30)


def _llm_lost_cost() -> CostEntry:
    body = request_body(LLM_MODEL, _llm_request(), "low")
    tokens = estimate_tokens(body, 10)
    return CostEntry("groq", LLM_MODEL, Purpose.EXTRACTION, tokens, PRICE.usd(tokens), 0)


async def _llm(fake: FakeGroq) -> None:
    llm = GroqLLM(
        httpx.AsyncClient(transport=httpx.MockTransport(fake)), Secret("k"), sleep=_sleep0
    )
    await llm.complete_json(_llm_request(), lambda raw: raw)


async def test_groq_json_a_lost_dispatch_keeps_one_estimate_on_400() -> None:
    fake = FakeGroq(httpx.ReadTimeout("lost", request=None), httpx.Response(400, text="bad"))
    with pytest.raises(LLMBadResponse) as e:
        await _llm(fake)
    assert (len(fake.requests), e.value.costs) == (2, (_llm_lost_cost(),))
    assert len(e.value.attempts) == 2


async def test_groq_json_a_lost_dispatch_keeps_one_estimate_on_auth() -> None:
    fake = FakeGroq(httpx.ReadTimeout("lost", request=None), httpx.Response(401, json={}))
    with pytest.raises(LLMAuthError) as e:
        await _llm(fake)
    assert (len(fake.requests), e.value.costs) == (2, (_llm_lost_cost(),))


async def test_groq_json_a_direct_refusal_bills_nothing() -> None:
    fake = FakeGroq(httpx.Response(400, text="bad"))
    with pytest.raises(LLMBadResponse) as e:
        await _llm(fake)
    assert (len(fake.requests), e.value.costs) == (1, ())
    fake2 = FakeGroq(httpx.Response(401, json={}))
    with pytest.raises(LLMAuthError) as e:
        await _llm(fake2)
    assert (len(fake2.requests), e.value.costs) == (1, ())
