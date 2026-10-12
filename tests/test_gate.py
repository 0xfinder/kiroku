"""The Gate without a database: Windows, state, Budget arithmetic, and the Jev adapter."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from kiroku.budget import Caps, OverBudget, Refusal, Unlimited, month_start, refusal
from kiroku.config import Secret
from kiroku.gate import (
    GateAuthError,
    GateBadResponse,
    GateRequest,
    GateUnavailable,
    Mode,
    rule_question,
)
from kiroku.gate.jev import JevGate, estimate_tokens, request_body, usd
from kiroku.gate.windows import Bounds, GateState, Turn, WindowPolicy, build_state, due_windows
from kiroku.ledger import Purpose, Tokens
from kiroku.rules import Rule

POLICY = WindowPolicy(length_ms=45_000, context_chars=40, context_lookback_ms=60_000)
RULE = Rule(7, "predicts_winner", "Does he predict the winner?", Decimal("0.60"), None, 600)
STATE = GateState({"stream": {"title": "t"}}, "I think Spirit wins this.")


# ---- Windows ---------------------------------------------------------------------------------


def test_windows_tile_the_clock_up_to_the_watermark() -> None:
    assert due_windows(0, 44_999, POLICY, final=False) == []
    assert due_windows(0, 100_000, POLICY, final=False) == [
        Bounds(0, 45_000),
        Bounds(45_000, 90_000),
    ]
    # A reopened session continues from the last end, not from a grid.
    assert due_windows(63_000, 108_000, POLICY, final=False) == [Bounds(63_000, 108_000)]


def test_only_a_session_end_closes_the_partial_window() -> None:
    assert due_windows(45_000, 63_000, POLICY, final=False) == []
    assert due_windows(45_000, 63_000, POLICY, final=True) == [Bounds(45_000, 63_000)]
    assert due_windows(45_000, 45_000, POLICY, final=True) == []


def test_state_keeps_earlier_speech_out_of_new() -> None:
    turns = [
        Turn(0, "too old to matter"),
        Turn(50_000, "it's like a 70-30 game, kinda."),
        Turn(80_000, "caster: Spirit take the fight", counts=False),
        Turn(90_000, "This Tide? Yikes."),
        Turn(134_999, "Still the same window."),
        Turn(135_000, "next window"),
    ]
    state = build_state(Bounds(90_000, 135_000), turns, {"title": "t"}, POLICY)
    assert state.new == "This Tide? Yikes. Still the same window."
    # The last 40 characters of earlier speech, cut at a word, and nothing from `new`.
    assert state.context == {
        "stream": {"title": "t"},
        "earlier": "kinda. caster: Spirit take the fight",
    }


def test_state_with_nothing_earlier_has_no_earlier_field() -> None:
    state = build_state(Bounds(0, 45_000), [Turn(10, "hello")], {"title": "t"}, POLICY)
    assert state.as_json() == {"context": {"stream": {"title": "t"}}, "new": "hello"}


def test_rule_question_judges_new_only() -> None:
    q = rule_question(RULE)
    assert q.question.startswith("In `new`: Does he predict the winner?\n")
    assert "if something is said only in `context`, the answer is no" in q.question
    assert (q.rule_id, q.name, q.threshold) == (7, "predicts_winner", Decimal("0.60"))


# ---- Budget arithmetic -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("month", "session", "estimate", "expected"),
    [
        ("19.99", "1.00", "0.01", None),  # exactly at the cap is allowed
        ("19.99", "1.00", "0.02", "month"),
        ("1.00", "1.49", "0.02", "session"),
        ("20.00", "1.50", "0.01", "month"),  # month is checked first
        ("1.00", None, "0.90", None),  # no session: only the month cap applies
    ],
)
def test_refusal(month: str, session: str | None, estimate: str, expected: str | None) -> None:
    got = refusal(
        Caps(session_usd=Decimal("1.50"), month_usd=Decimal("20")),
        month_spent=Decimal(month),
        session_spent=Decimal(session) if session else None,
        estimate=Decimal(estimate),
    )
    assert (got.cap if got else None) == expected


def test_month_starts_at_utc_midnight_on_the_first() -> None:
    late = datetime(2026, 11, 1, 3, 0, tzinfo=UTC).astimezone()
    assert month_start(late) == datetime(2026, 11, 1, tzinfo=UTC)


# ---- Jev adapter -----------------------------------------------------------------------------


def jev_body(p: float = 0.72, *, tokens: int = 700) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "mode": {
                "type": "choice",
                "choice": "watching_pro",
                "confidence": 0.8,
                "probabilities": {"playing": 0.05, "watching_pro": 0.9, "chatting": 0.05},
            },
            "rule_7": {"type": "noul", "noul": p},
        },
        "usage": {"input_tokens": tokens, "output_tokens": 40},
    }


class FakeJev:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def gate(fake: FakeJev, clock: Clock | None = None) -> JevGate:
    clock = clock or Clock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return JevGate(http, Secret("jev_test"), sleep=clock.sleep, clock=clock)


def request(guard: Any = None) -> GateRequest:
    return GateRequest(STATE, (rule_question(RULE),), guard or Unlimited())


async def test_one_request_asks_mode_and_every_rule_about_the_split_state() -> None:
    fake = FakeJev(httpx.Response(200, json=jev_body()))
    answer = await gate(fake).evaluate(request())
    sent = json.loads(fake.requests[0].content)
    assert sent["model"] == "jev-1.13.0"
    assert sent["state"] == {"context": {"stream": {"title": "t"}}, "new": STATE.new}
    assert set(sent["questions"]) == {"mode", "rule_7"}
    assert sent["questions"]["rule_7"]["type"] == "noul"
    assert fake.requests[0].headers["authorization"] == "Bearer jev_test"
    assert answer.model == "jev:jev-1.13.0"
    assert answer.probabilities == {7: 0.72}
    assert (answer.mode, answer.mode_probability) == (Mode.WATCHING_PRO, 0.9)
    (cost,) = answer.costs
    assert (cost.provider, cost.purpose, cost.usage) == ("jev", Purpose.GATE, Tokens(700, 40))
    assert cost.usd == Decimal("0.000030")  # 700 x $0.042/M = 0.0000294, rounded up


def test_estimate_is_never_below_the_billed_tokens() -> None:
    # Every request recorded live for issue #3, against what Jev billed for it.
    path = Path("spikes/results/2026-10-11-gate-split.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()][1:]
    for row in rows:
        assert estimate_tokens(row["request"]) >= row["response"]["usage"]["input_tokens"]


async def test_429_waits_retry_after_and_pauses_every_session() -> None:
    clock = Clock()
    fake = FakeJev(
        httpx.Response(429, headers={"retry-after": "3"}),
        httpx.Response(200, json=jev_body()),
    )
    g = gate(fake, clock)
    await g.evaluate(request())
    assert clock.sleeps == [3.0]
    # Another session's request also waits for the shared pause. Rewind the clock to just
    # after the 429 to see it.
    clock.now -= 2.0
    await g.evaluate(request())
    assert clock.sleeps[-1] == pytest.approx(2.0)


async def test_overloaded_then_ok_bills_once() -> None:
    fake = FakeJev(
        httpx.Response(529),
        httpx.Response(503),
        httpx.Response(200, json=jev_body()),
    )
    answer = await gate(fake).evaluate(request())
    assert len(fake.requests) == 3
    assert [c.usage for c in answer.costs] == [Tokens(700, 40)]


async def test_gives_up_after_the_deadline() -> None:
    clock = Clock()
    with pytest.raises(GateUnavailable, match="HTTP 529") as e:
        await gate(FakeJev(httpx.Response(529)), clock).evaluate(request())
    assert e.value.costs == ()  # a 529 is an answer: nothing was billed
    assert sum(clock.sleeps) <= 60 + 8


async def test_lost_responses_are_billed_from_the_estimate() -> None:
    req = httpx.Request("POST", "https://x")
    fake = FakeJev(httpx.ReadTimeout("lost", request=req), httpx.Response(200, json=jev_body()))
    answer = await gate(fake).evaluate(request())
    # The lost call may have been billed too: count it at this call's size.
    assert [c.usage for c in answer.costs] == [Tokens(1400, 80)]


async def test_a_wrong_key_raises_auth_error_without_retrying() -> None:
    fake = FakeJev(httpx.Response(401))
    with pytest.raises(GateAuthError):
        await gate(fake).evaluate(request())
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"answers": {}, "usage": {"input_tokens": 700}},  # rule key missing
        jev_body(1.3),
        jev_body(float("nan")),
        {**jev_body(), "answers": {**jev_body()["answers"], "rule_7": {"noul": True}}},
        {**jev_body(), "answers": {**jev_body()["answers"], "mode": {"choice": "sleeping"}}},
        [],
    ],
)
async def test_malformed_answers_are_not_decisions(body: Any) -> None:
    content = json.dumps(body).encode()
    fake = FakeJev(httpx.Response(200, content=content))
    with pytest.raises(GateBadResponse) as e:
        await gate(fake).evaluate(request())
    # The call was answered, so it was probably billed: the ledger gets the estimate.
    assert len(e.value.costs) == 1


async def test_a_bad_request_is_not_retried_or_billed() -> None:
    fake = FakeJev(httpx.Response(400, text="bad question"))
    with pytest.raises(GateBadResponse, match="bad question") as e:
        await gate(fake).evaluate(request())
    assert (len(fake.requests), e.value.costs) == (1, ())


async def test_over_budget_sends_nothing() -> None:
    class Refuse:
        async def check(self, estimate_usd: Decimal) -> None:
            raise OverBudget(Refusal("session", Decimal(0), Decimal(0), estimate_usd))

    fake = FakeJev(httpx.Response(200, json=jev_body()))
    with pytest.raises(OverBudget):
        await gate(fake).evaluate(request(Refuse()))
    assert fake.requests == []


def test_a_rule_test_asks_no_mode() -> None:
    body = request_body(
        "m", GateRequest(STATE, (rule_question(RULE),), Unlimited(), ask_mode=False)
    )
    assert set(body["questions"]) == {"rule_7"}  # type: ignore[arg-type]


def test_usd_rounds_up() -> None:
    assert usd(1) == Decimal("0.000001")
    assert usd(0) == Decimal(0)


async def test_a_long_retry_after_gives_up_instead_of_stalling() -> None:
    clock = Clock()
    fake = FakeJev(httpx.Response(429, headers={"retry-after": "3600"}))
    g = gate(fake, clock)
    with pytest.raises(GateUnavailable, match="HTTP 429"):
        await g.evaluate(request())
    assert clock.sleeps == []
    # Other sessions honour the pause too, without sending anything.
    with pytest.raises(GateUnavailable, match="rate limited"):
        await g.evaluate(request())
    assert len(fake.requests) == 1


async def test_an_undecodable_body_is_retried_like_a_lost_response() -> None:
    req = httpx.Request("POST", "https://x")
    fake = FakeJev(
        httpx.DecodingError("bad gzip", request=req), httpx.Response(200, json=jev_body())
    )
    answer = await gate(fake).evaluate(request())
    assert [c.usage for c in answer.costs] == [Tokens(1400, 80)]


@pytest.mark.parametrize(
    ("text", "chars", "expected"),
    [
        ("aa bbb ccc", 7, "bbb ccc"),  # the cut lands on a word start: keep it
        ("aa bbb ccc", 6, "ccc"),  # mid-word: drop the fragment
        ("abcdef", 3, "def"),  # no space to cut at
    ],
)
def test_context_tail_never_drops_a_whole_word(text: str, chars: int, expected: str) -> None:
    policy = WindowPolicy(context_chars=chars)
    state = build_state(Bounds(60_000, 105_000), [Turn(30_000, text)], {}, policy)
    assert state.context["earlier"] == expected


def test_a_probability_at_the_threshold_passes_like_the_database_says() -> None:
    from kiroku.gate import passes

    assert Decimal("0.7") > 0.7  # the trap: a float is not exactly 0.7
    assert passes(0.7, Decimal("0.70")) and passes(0.6, Decimal("0.6"))
    assert not passes(0.69994, Decimal("0.7"))
