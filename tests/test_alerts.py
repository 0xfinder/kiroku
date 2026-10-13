"""Failure contracts at the paid-provider and Telegram boundaries. No live services."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from kiroku.budget import OverBudget, Refusal, Unlimited
from kiroku.config import Secret
from kiroku.extraction import parse_extraction
from kiroku.gate import GateRequest, GateUnavailable, Mode, RuleQuestion
from kiroku.gate.fallback import FallbackGate
from kiroku.gate.llm import LLMGate
from kiroku.gate.windows import GateState
from kiroku.ledger import CostEntry, Purpose, Tokens
from kiroku.llm import LLMBadResponse, LLMRequest, LLMUnavailable
from kiroku.llm.groq import GroqLLM
from kiroku.notify import AlertMessage, NotifierRejected, NotifierStale, NotifierUnsure
from kiroku.notify.telegram import TelegramNotifier, keyboard, parse_tap, render_html


class Clock:
    value = 0.0

    def __init__(self) -> None:
        self.waits: list[float] = []

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.value += seconds


def message() -> AlertMessage:
    return AlertMessage(
        7,
        "gorgc",
        "predicts_winner",
        "Spirit 70-30",
        None,
        datetime.now(UTC),
        Decimal(".68"),
        "jev:jev-1.13.0",
    )


def completion(content: object, *, finish: str = "stop") -> dict[str, Any]:
    return {
        "model": "openai/gpt-oss-120b",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}],
    }


def request() -> LLMRequest:
    return LLMRequest("system", "{}", Purpose.EXTRACTION, Unlimited(), 1024, 60)


def parse(raw: Mapping[str, Any]) -> str:
    value = raw["claim"]
    if not isinstance(value, str):
        raise ValueError("claim")
    return value


@pytest.mark.parametrize(
    "body", [completion({}), completion({"claim": "yes"}, finish="length"), {"choices": []}]
)
async def test_bad_json_or_schema_retries_once_and_keeps_both_costs(body: dict[str, Any]) -> None:
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LLMBadResponse) as error:
            await GroqLLM(http, Secret("k")).complete_json(request(), parse)
    assert calls == 2
    assert len(error.value.costs) == 2


async def test_invalid_json_400_is_billed_and_retried_once() -> None:
    count = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(400, json={"error": {"code": "json_validate_failed"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LLMBadResponse) as e:
            await GroqLLM(http, Secret("k")).complete_json(request(), parse)
    assert count == len(e.value.costs) == 2


async def test_llm_rate_limit_pause_is_shared_and_daily_quota_reserved() -> None:
    clock = Clock()
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "600"})
        return httpx.Response(
            200,
            json=completion({"claim": "yes"}),
            headers={"x-ratelimit-remaining-requests": "149"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        llm = GroqLLM(http, Secret("k"), sleep=clock.sleep, clock=clock.now)
        with pytest.raises(LLMUnavailable):
            await llm.complete_json(request(), parse)
        with pytest.raises(LLMUnavailable):
            await llm.complete_json(request(), parse)
        assert calls == 1 and clock.waits == []
        clock.value = 600
        await llm.complete_json(request(), parse)
        with pytest.raises(LLMUnavailable, match="reserved"):
            await llm.complete_json(
                LLMRequest("system", "{}", Purpose.GATE, Unlimited(), 512, 20, 150), parse
            )
        assert calls == 2


class Refusing:
    async def check(self, estimate_usd: Decimal) -> None:
        raise OverBudget(Refusal("month", Decimal("0"), Decimal("0"), estimate_usd))


async def test_budget_refuses_before_llm_http() -> None:
    def respond(req: httpx.Request) -> httpx.Response:
        pytest.fail("refused call reached HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(OverBudget):
            await GroqLLM(http, Secret("k")).complete_json(
                LLMRequest("s", "{}", Purpose.EXTRACTION, Refusing(), 1024, 60), parse
            )


async def test_fallback_budget_refusal_preserves_primary_lost_costs() -> None:
    cost = CostEntry("jev", "jev-1.13.0", Purpose.GATE, Tokens(12), Decimal(".000001"), 0)

    class Down:
        name = "jev"

        async def evaluate(self, request: GateRequest) -> Any:
            raise GateUnavailable("jev unavailable", (cost,))

    def respond(req: httpx.Request) -> httpx.Response:
        pytest.fail("refused fallback reached HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        gate = FallbackGate(Down(), LLMGate(GroqLLM(http, Secret("k"))))
        with pytest.raises(OverBudget) as e:
            await gate.evaluate(GateRequest(GateState({}, "new"), (), Refusing()))
    assert e.value.costs == (cost,)


async def test_telegram_429_honours_retry_after_and_paces_sends() -> None:
    clock = Clock()
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 5}})
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": calls, "chat": {"id": 42}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        bot = TelegramNotifier(http, Secret("TOKEN"), 42, sleep=clock.sleep, clock=clock.now)
        await bot.send(message(), give_up_s=20)
        await bot.send(message(), give_up_s=20)
    assert clock.waits == [5, 1]
    assert calls == 3


@pytest.mark.parametrize("failure", ["5xx", "timeout", "malformed", "malformed429"])
async def test_unsure_delivery_is_not_retried(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("https://api.telegram.org/botTOKEN/sendMessage", request=req)
        if failure == "5xx":
            return httpx.Response(502)
        if failure == "malformed429":
            return httpx.Response(429, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(NotifierUnsure) as e:
            await TelegramNotifier(http, Secret("TOKEN"), 42).send(message(), give_up_s=20)
    assert calls == 1
    assert "TOKEN" not in str(e.value)
    assert "TOKEN" not in caplog.text


async def test_connect_failure_has_a_definite_delivery_deadline() -> None:
    clock = Clock()

    def respond(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("TOKEN", request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(NotifierStale):
            await TelegramNotifier(
                http, Secret("TOKEN"), 42, sleep=clock.sleep, clock=clock.now
            ).send(message(), give_up_s=7)
    assert clock.waits == [1]


async def test_blocked_bot_is_rejected_and_sanitized() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(403, json={"ok": False, "description": "blocked TOKEN 42"})
        )
    ) as http:
        with pytest.raises(NotifierRejected) as e:
            await TelegramNotifier(http, Secret("TOKEN"), 42).send(message(), give_up_s=10)
    assert str(e.value) == "blocked <BOT_TOKEN> <CHAT_ID>"


def test_hallucinated_quote_is_dropped_and_no_previous_forces_new() -> None:
    value = parse_extraction(
        {
            "claim": "Spirit wins",
            "summary": "Spirit wins",
            "quote": "invented",
            "same_as_previous": True,
        },
        new="70-30 for Spirit",
        has_previous=False,
    )
    assert value.quote is None and value.same_as_previous is False
    value = parse_extraction(
        {
            "claim": "Spirit wins",
            "summary": "Spirit wins",
            "quote": "70-30 FOR Spirit",
            "same_as_previous": False,
        },
        new="70-30 for  Spirit",
        has_previous=True,
    )
    assert value.quote == "70-30 FOR Spirit"


def test_transcript_html_is_escaped() -> None:
    value = AlertMessage(
        1, "<b>&", "</i>", "<b>&", "</i>", datetime.now(UTC), Decimal(".7"), "jev:x"
    )
    html = render_html(value)
    assert "&lt;b&gt;&amp;" in html and "&lt;/i&gt;" in html
    assert "<b>&</b>" not in html
    assert keyboard(2)["inline_keyboard"] == [
        [{"text": "👍", "callback_data": "l:2:y"}, {"text": "👎", "callback_data": "l:2:n"}]
    ]


def test_foreign_and_invalid_callbacks_are_not_labels() -> None:
    update: dict[str, Any] = {
        "callback_query": {
            "id": "q",
            "message": {"chat": {"id": 42}, "date": 1, "message_id": 7},
            "data": "l:1:y",
        }
    }
    assert parse_tap(update, 99) is None
    update["callback_query"]["data"] = "l:9999999999999999999:y"
    assert parse_tap(update, 42) is None
    update["callback_query"]["data"] = "l:1:y"
    tap = parse_tap(update, 42)
    assert tap is not None and tap.applies and tap.delivery.external_id == "42:7"


@pytest.mark.parametrize("raw", [True, "0.5", -0.1, 1.1, float("nan"), 10**400])
async def test_llm_gate_rejects_invalid_probabilities(raw: object) -> None:
    answer = {"answers": {"rule_1": raw}, "mode": {"choice": Mode.CHATTING, "probability": 1}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=completion(answer)))
    ) as http:
        from kiroku.gate import GateBadResponse

        with pytest.raises(GateBadResponse):
            await LLMGate(GroqLLM(http, Secret("k"))).evaluate(
                GateRequest(
                    GateState({}, "new"),
                    (RuleQuestion(1, "r", "question", Decimal(".6")),),
                    Unlimited(),
                )
            )


async def test_daily_quota_cache_expires_and_concurrent_fallback_preserves_reserve() -> None:
    import asyncio

    clock = Clock()
    calls = 0

    async def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        remaining = max(150 - calls, 0)
        return httpx.Response(
            200,
            json=completion({"claim": "yes"}),
            headers={
                "x-ratelimit-remaining-requests": str(remaining),
                "x-ratelimit-reset-requests": "1s",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        llm = GroqLLM(http, Secret("k"), clock=clock.now)
        await llm.complete_json(request(), parse)
        fallback = LLMRequest("s", "{}", Purpose.GATE, Unlimited(), 512, 20, 150)
        refused = await asyncio.gather(
            *(llm.complete_json(fallback, parse) for _ in range(3)), return_exceptions=True
        )
        assert all(isinstance(r, LLMUnavailable) for r in refused)
        assert calls == 1
        clock.value = 2
        await llm.complete_json(fallback, parse)
        assert calls == 2


async def test_auth_failure_preserves_previous_billed_attempt() -> None:
    from kiroku.llm import LLMAuthError

    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=completion({})) if calls == 1 else httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LLMAuthError) as error:
            await GroqLLM(http, Secret("k")).complete_json(request(), parse)
    assert len(error.value.costs) == 1 and calls == 2


async def test_jev_auth_error_does_not_fall_back() -> None:
    from kiroku.gate import GateAuthError

    class BadKey:
        name = "jev"

        async def evaluate(self, request: GateRequest) -> Any:
            raise GateAuthError("wrong key")

    def respond(req: httpx.Request) -> httpx.Response:
        pytest.fail("auth error reached fallback")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(GateAuthError):
            await FallbackGate(BadKey(), LLMGate(GroqLLM(http, Secret("k")))).evaluate(
                GateRequest(GateState({}, "new"), (), Unlimited())
            )


async def test_foreign_chat_callback_is_acknowledged_without_yielding() -> None:
    import asyncio

    from tests.fakes.telegram import BotAPI

    bot_api = BotAPI()
    bot_api.tap(7, chat_id=99)
    methods: list[str] = []

    async def respond(req: httpx.Request) -> httpx.Response:
        methods.append(req.url.path.rsplit("/", 1)[-1])
        response = bot_api(req)
        if methods.count("getUpdates") > 1:
            raise asyncio.CancelledError
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        iterator = TelegramNotifier(http, Secret("TOKEN"), 42).taps()
        with pytest.raises(asyncio.CancelledError):
            await anext(iterator)
        await iterator.aclose()
    assert methods == ["getUpdates", "answerCallbackQuery", "getUpdates"]
    assert bot_api.updates == []


async def test_slow_extraction_does_not_occupy_fallback_quota_lock() -> None:
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["max_completion_tokens"] == 1024:
            entered.set()
            await release.wait()
        return httpx.Response(200, json=completion({"claim": "yes"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        llm = GroqLLM(http, Secret("k"))
        pending = asyncio.create_task(llm.complete_json(request(), parse))
        await entered.wait()
        try:
            result = await asyncio.wait_for(
                llm.complete_json(
                    LLMRequest("s", "{}", Purpose.GATE, Unlimited(), 512, 20, 150), parse
                ),
                1,
            )
            assert result.value == "yes"
        finally:
            release.set()
            await pending


async def test_connect_failure_does_not_spend_reserved_daily_requests() -> None:
    clock = Clock()
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise httpx.ConnectError("offline", request=req)
        return httpx.Response(
            200,
            json=completion({"claim": "yes"}),
            headers={"x-ratelimit-remaining-requests": "151", "x-ratelimit-reset-requests": "24h"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        llm = GroqLLM(http, Secret("k"), clock=clock.now, sleep=clock.sleep)
        await llm.complete_json(request(), parse)
        result = await llm.complete_json(
            LLMRequest("s", "{}", Purpose.GATE, Unlimited(), 512, 20, 150), parse
        )
    assert result.value == "yes" and calls == 3


async def test_fallback_auth_disables_only_fallback_and_primary_can_recover(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from kiroku.gate import GateAnswer, GateAuthError, GateFailed

    cost = CostEntry("jev", "1", Purpose.GATE, Tokens(10), Decimal(".000005"), 0)

    class Primary:
        name = "jev"
        calls = 0

        async def evaluate(self, request: GateRequest) -> GateAnswer:
            self.calls += 1
            if self.calls < 3:
                raise GateUnavailable("jev unavailable", (cost,))
            return GateAnswer("jev:1", {}, Mode.CHATTING, 1, ())

    class Fallback:
        name = "groq"
        calls = 0

        async def evaluate(self, request: GateRequest) -> GateAnswer:
            self.calls += 1
            raise GateAuthError("wrong fallback key", (cost,))

    primary, fallback = Primary(), Fallback()
    gate = FallbackGate(primary, fallback)
    req = GateRequest(GateState({}, "new"), (), Unlimited())
    with pytest.raises(GateFailed, match="authentication") as first:
        await gate.evaluate(req)
    assert first.value.costs == (cost, cost)
    with pytest.raises(GateFailed, match="disabled") as second:
        await gate.evaluate(req)
    assert second.value.costs == (cost,)
    assert (await gate.evaluate(req)).model == "jev:1"
    assert primary.calls == 3 and fallback.calls == 1
    assert sum(r.levelname == "ERROR" for r in caplog.records) == 1


@pytest.mark.parametrize("deadline", [0.001, 4.99, 5.99])
async def test_near_deadline_does_not_start_a_paced_send(deadline: float) -> None:
    clock = Clock()
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert req.extensions["timeout"]["read"] == 15.0
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": calls, "chat": {"id": 42}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        bot = TelegramNotifier(http, Secret("TOKEN"), 42, clock=clock.now, sleep=clock.sleep)
        await bot.send(message(), give_up_s=5)
        with pytest.raises(NotifierStale):
            await bot.send(message(), give_up_s=deadline)
    assert calls == 1 and clock.waits == []


async def test_started_send_can_finish_after_stale_deadline() -> None:
    clock = Clock()

    def respond(req: httpx.Request) -> httpx.Response:
        assert req.extensions["timeout"]["read"] == 15.0
        clock.value += 10
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": 42}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await TelegramNotifier(http, Secret("TOKEN"), 42, clock=clock.now).send(
            message(), give_up_s=5
        )
    assert result.external_id == "42:7"


@pytest.mark.parametrize("date", [None, 0, 1])
def test_callback_unknown_date_uses_receipt_time(date: int | None) -> None:
    now = datetime(2026, 10, 12, tzinfo=UTC)
    message: dict[str, Any] = {"chat": {"id": 42}, "message_id": 7}
    if date is not None:
        message["date"] = date
    tap = parse_tap(
        {"callback_query": {"id": "q", "message": message, "data": "l:1:y"}},
        42,
        now=lambda: now,
    )
    assert tap is not None
    assert tap.sent_at == (now if not date else datetime.fromtimestamp(date, UTC))


async def test_llm_token_limit_waits_for_minute_reset_not_daily_requests() -> None:
    clock = Clock()
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={
                    "x-ratelimit-remaining-tokens": "0",
                    "x-ratelimit-reset-tokens": "2s",
                    "x-ratelimit-remaining-requests": "999",
                    "x-ratelimit-reset-requests": "1h20m",
                },
            )
        return httpx.Response(200, json=completion({"claim": "yes"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        llm = GroqLLM(http, Secret("k"), clock=clock.now, sleep=clock.sleep)
        assert (await llm.complete_json(request(), parse)).value == "yes"
        assert (await llm.complete_json(request(), parse)).value == "yes"
    assert calls == 3 and clock.waits == [2]


@pytest.mark.parametrize("remaining", [300, 301])
async def test_notes_respects_exact_quota_reserve(remaining: int) -> None:
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=completion({"claim": "yes"}),
            headers={
                "x-ratelimit-remaining-requests": str(remaining),
                "x-ratelimit-reset-requests": "24h",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        llm = GroqLLM(http, Secret("k"))
        await llm.complete_json(request(), parse)
        req = LLMRequest("s", "{}", Purpose.NOTES, Unlimited(), 512, 20, 300)
        if remaining == 300:
            with pytest.raises(LLMUnavailable, match="reserved"):
                await llm.complete_json(req, parse)
        else:
            assert (await llm.complete_json(req, parse)).value == "yes"
    assert calls == (1 if remaining == 300 else 2)


async def test_cancel_during_retry_budget_check_keeps_paid_attempt() -> None:
    import asyncio

    from kiroku.llm import LLMCancelled

    entered = asyncio.Event()

    class Guard:
        calls = 0

        async def check(self, estimate_usd: Decimal) -> None:
            self.calls += 1
            if self.calls == 2:
                entered.set()
                await asyncio.Event().wait()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=completion({})),
        )
    ) as http:
        llm = GroqLLM(http, Secret("k"))
        req = LLMRequest("s", "{}", Purpose.NOTES, Guard(), 512, 20)
        task = asyncio.create_task(llm.complete_json(req, parse))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(LLMCancelled) as error:
            await task
    assert len(error.value.costs) == 1 and len(error.value.attempts) == 1


@pytest.mark.parametrize("dispatched", [False, True])
async def test_cancel_costs_only_dispatched_request(
    dispatched: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from kiroku.llm import LLMCancelled

    entered = asyncio.Event()

    async def respond(req: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled request should not return")

    class Guard:
        async def check(self, estimate_usd: Decimal) -> None:
            if not dispatched:
                entered.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        llm = GroqLLM(http, Secret("k"))
        lock = asyncio.Lock()
        if not dispatched:
            await lock.acquire()
            monkeypatch.setattr(llm, "_admission", lock)
        req = LLMRequest("s", "{}", Purpose.NOTES, Guard(), 512, 20)
        task = asyncio.create_task(llm.complete_json(req, parse))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(LLMCancelled) as error:
            await task
        if not dispatched:
            lock.release()
    assert len(error.value.costs) == int(dispatched)
    assert len(error.value.attempts) == int(dispatched)
