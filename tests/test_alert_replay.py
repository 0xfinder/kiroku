"""Issue #5 acceptance and durable failure behavior through production code and Postgres."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from psycopg import AsyncConnection

from kiroku import alerting, labels, rules, sessions
from kiroku.budget import Budget, Caps
from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.gate.fallback import FallbackGate
from kiroku.gate.jev import JevGate
from kiroku.gate.llm import LLMGate
from kiroku.ledger import CostEntry, Purpose, Tokens
from kiroku.llm.groq import GroqLLM
from kiroku.notify import Delivery, Tap
from kiroku.notify.telegram import TelegramNotifier
from tests.fakes.alerts import Sink, replay_transport, seed
from tests.fakes.telegram import BotAPI
from tests.test_alerts import Clock, completion

EXTRACTION = "spikes/results/2026-10-12-alert-extraction.jsonl"
FALLBACK = "spikes/results/2026-10-12-gate-fallback.jsonl"


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


async def worker(
    pool: Pool, channel: int, now: datetime, *, arm: str = "default", repeat: int = 0
) -> Sink:
    bot = BotAPI()
    clock = Clock()
    async with (
        httpx.AsyncClient(transport=replay_transport(EXTRACTION, arm=arm, repeat=repeat)) as http,
        httpx.AsyncClient(transport=httpx.MockTransport(bot)) as bot_http,
    ):
        sink = Sink(
            TelegramNotifier(bot_http, Secret("TOKEN"), 42, sleep=clock.sleep, clock=clock.now)
        )
        await alerting.AlertWorker(
            pool, [channel], GroqLLM(http, Secret("k")), sink, Budget(pool, Caps()), now=lambda: now
        ).drain()
    return sink


@pytest.mark.parametrize("repeat", [0, 1, 2])
async def test_one_alert_for_the_70_30_call(
    pool: Pool, database_url: str, conn: AsyncConnection, repeat: int
) -> None:
    channel, _, now = await seed(pool, database_url)
    sink = await worker(pool, channel, now, repeat=repeat)
    predictions = [m for m in sink.messages if m.rule == "predicts_winner"]
    assert len(predictions) == 1
    assert "70" in predictions[0].summary and "Spirit" in predictions[0].summary
    assert len(sink.messages) == 3
    rows = await (
        await conn.execute("""SELECT w.start_ms/30000, a.outcome FROM alert a
        JOIN gate_decision d ON d.id=a.decision_id JOIN gate_window w ON w.id=d.window_id
        JOIN rule r ON r.id=a.rule_id WHERE r.name='predicts_winner' ORDER BY w.start_ms""")
    ).fetchall()
    assert rows == [(0, "sent"), (7, "cooldown")]
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose='extraction'")
    ).fetchone() == (3,)


@pytest.mark.parametrize("repeat", [0, 1, 2])
async def test_novelty_drops_the_restated_prediction(
    pool: Pool, database_url: str, conn: AsyncConnection, repeat: int
) -> None:
    channel, _, now = await seed(pool, database_url, cooldown=0)
    sink = await worker(pool, channel, now, arm="novelty_only", repeat=repeat)
    assert len([m for m in sink.messages if m.rule == "predicts_winner"]) == 1
    rows = await (
        await conn.execute("""SELECT w.start_ms/30000, a.outcome, a.id, a.previous_alert_id
        FROM alert a
        JOIN gate_decision d ON d.id=a.decision_id JOIN gate_window w ON w.id=d.window_id
        JOIN rule r ON r.id=a.rule_id WHERE r.name='predicts_winner' ORDER BY w.start_ms""")
    ).fetchall()
    assert rows[0][0:2] == (0, "sent")
    assert rows[1][0:2] == (7, "duplicate")
    assert rows[1][3] == rows[0][2]


def telegram_fixture() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(Path("tests/fixtures/telegram/smoke.json").read_text())
    synthesized: dict[str, Any] = json.loads(
        Path("tests/fixtures/telegram/synthesized.json").read_text()
    )
    raw["calls"] += synthesized["calls"]

    # The checked-in fixture has placeholders; CI uses a public dummy chat.
    def restore(value: Any) -> Any:
        if value == "<CHAT_ID>":
            return 42
        if isinstance(value, dict):
            return {k: restore(v) for k, v in cast(dict[str, Any], value).items()}
        if isinstance(value, list):
            return [restore(v) for v in cast(list[Any], value)]
        return value

    return restore(raw)


async def test_tap_labels_the_gate_decision(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, now = await seed(pool, database_url)
    sink = await worker(pool, channel, now)
    message = next(m for m in sink.messages if m.rule == "predicts_winner")
    fixture = telegram_fixture()
    sent = next(r["response"] for r in fixture["calls"] if r["method"] == "sendMessage")
    update = next(
        u
        for r in fixture["calls"]
        if r["method"] == "getUpdates"
        for u in r["response"]["result"]
        if "callback_query" in u
    )
    update["callback_query"]["data"] = f"l:{message.alert_id}:y"
    sent["result"]["message_id"] = message.alert_id
    update["callback_query"]["message"]["message_id"] = message.alert_id
    methods: list[str] = []

    def respond(req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": [update]})
        if method == "sendMessage":
            return httpx.Response(200, json=sent)
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        bot = TelegramNotifier(http, Secret("TOKEN"), 42)
        taps = bot.taps()
        tap = await anext(taps)
        assert await labels.record(pool, tap)
        await bot.confirm(tap, recorded=True)
        await taps.aclose()
    row = await (
        await conn.execute(
            "SELECT decision_id, applies, source FROM label WHERE alert_id=%s", [message.alert_id]
        )
    ).fetchone()
    decision = await (
        await conn.execute("SELECT decision_id FROM alert WHERE id=%s", [message.alert_id])
    ).fetchone()
    assert row == (decision[0], True, "telegram") if decision else False
    assert "answerCallbackQuery" in methods and "editMessageReplyMarkup" in methods


async def test_jev_down_falls_back_and_logs_it(
    pool: Pool, database_url: str, conn: AsyncConnection, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    primary_calls = 0

    def down(req: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        raise httpx.ConnectError("offline", request=req)

    with caplog.at_level(logging.WARNING):
        transport = httpx.MockTransport(down)
        async with (
            httpx.AsyncClient(transport=transport) as jev_http,
            httpx.AsyncClient(transport=replay_transport(FALLBACK)) as llm_http,
        ):
            gate = FallbackGate(
                JevGate(jev_http, Secret("k"), give_up_s=0, clock=clock.now),
                LLMGate(GroqLLM(llm_http, Secret("k"))),
                clock=clock.now,
            )
            await seed(pool, database_url, gate=gate)
    assert primary_calls == 1
    assert len([r for r in caplog.records if "falling back" in r.message]) == 1
    windows = await (
        await conn.execute(
            "SELECT start_ms/30000, status, model, reason FROM gate_window ORDER BY start_ms"
        )
    ).fetchall()
    assert len(windows) == 8
    assert all(
        r[1] == "answered"
        and r[2] == "groq:openai/gpt-oss-120b"
        and r[3].startswith("fallback: jev unavailable")
        for r in windows
    )
    assert await (
        await conn.execute(
            "SELECT count(*) FROM cost_ledger WHERE purpose='gate' AND provider='groq'"
        )
    ).fetchone() == (8,)
    recording = await asyncio.to_thread(
        lambda: [json.loads(line) for line in Path(FALLBACK).read_text().splitlines()]
    )
    expected = {
        (r["chunk"], name): Decimal(str(score))
        >= Decimal(".6" if name == "predicts_winner" else ".7")
        for r in recording
        if r.get("repeat") == 0 and "scores" in r
        for name, score in r["scores"].items()
    }
    decisions = await (
        await conn.execute("""SELECT w.start_ms/30000, r.name, d.passed FROM gate_decision d
        JOIN gate_window w ON w.id=d.window_id JOIN rule r ON r.id=d.rule_id""")
    ).fetchall()
    assert {(chunk, name): passed for chunk, name, passed in decisions} == expected


async def test_stale_backlog_sends_nothing(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, now = await seed(pool, database_url, fresh=False)
    sink = await worker(pool, channel, now)
    assert sink.messages == []
    assert await (await conn.execute("SELECT DISTINCT outcome FROM alert")).fetchall() == [
        ("stale",)
    ]
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose='extraction'")
    ).fetchone() == (0,)


async def test_redelivered_tap_is_one_label_and_changed_mind_wins(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, now = await seed(pool, database_url)
    sink = await worker(pool, channel, now)
    m = sink.messages[0]
    tap = Tap(m.alert_id, True, None, Delivery(f"42:{m.alert_id}"), now)
    assert await labels.record(pool, tap)
    timestamp = await (await conn.execute("SELECT labelled_at FROM label")).fetchone()
    assert await labels.record(pool, tap)
    assert await (await conn.execute("SELECT labelled_at FROM label")).fetchone() == timestamp
    assert await labels.record(pool, replace(tap, applies=False))
    assert await (await conn.execute("SELECT applies, source FROM label")).fetchall() == [
        (False, "telegram")
    ]
    assert not await labels.record(pool, replace(tap, alert_id=99999))


async def test_crash_after_send_never_resends_and_callback_confirms_delivery(
    pool: Pool, database_url: str, conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel, _, now = await seed(pool, database_url)

    class Crash(BaseException):
        pass

    async def crash(pool: Pool, alert_id: int, delivery: Delivery, at: datetime) -> None:
        raise Crash("crash after send")

    monkeypatch.setattr(alerting, "mark_sent", crash)
    with pytest.raises(Crash):
        await worker(pool, channel, now)
    assert await alerting.recover(pool, [channel]) == 1
    monkeypatch.undo()
    sink = await worker(pool, channel, now)
    assert not any(m.rule == "predicts_winner" for m in sink.messages)
    row = await (await conn.execute("SELECT id FROM alert WHERE outcome='unconfirmed'")).fetchone()
    assert row
    tap = Tap(row[0], False, None, Delivery("42:99"), now)
    assert await labels.record(pool, tap)
    assert await (
        await conn.execute("SELECT outcome, external_id, sent_at FROM alert WHERE id=%s", [row[0]])
    ).fetchone() == ("sent", "42:99", now)
    assert await (await conn.execute("SELECT applies FROM label")).fetchone() == (False,)


async def test_repeated_commit_is_noop(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, _ = await seed(pool, database_url)
    candidate = (await alerting.pending(pool, [channel]))[0]
    cost = CostEntry("groq", "model", Purpose.EXTRACTION, Tokens(10, 20), Decimal(".001"), 0)
    decided = alerting.Sending("game", {"summary": "body"}, None, "model", "telegram", (cost,))
    assert await alerting.commit(pool, candidate, decided) is not None
    assert await alerting.commit(pool, candidate, decided) is None
    assert await (await conn.execute("SELECT count(*) FROM alert")).fetchone() == (1,)
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose='extraction'")
    ).fetchone() == (1,)


async def test_cooldown_is_per_channel(pool: Pool, database_url: str) -> None:
    first, _, now = await seed(pool, database_url)
    assert len((await worker(pool, first, now)).messages) == 3
    second, _, now = await seed(pool, database_url, login="caedrel")
    # The input's streamer field is intentionally different; use a local response here.
    sink = Sink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json=completion(
                    {"claim": "new", "summary": "new", "quote": None, "same_as_previous": False}
                ),
            )
        )
    ) as http:
        await alerting.AlertWorker(
            pool, [second], GroqLLM(http, Secret("k")), sink, Budget(pool, Caps()), now=lambda: now
        ).drain()
    assert len(sink.messages) == 3


async def test_stored_wording_survives_rule_edit_and_disable(pool: Pool, database_url: str) -> None:
    channel, _, now = await seed(pool, database_url)
    await rules.edit(pool, "predicts_winner", instructions="completely different question")
    await rules.set_enabled(pool, "predicts_winner", False)
    sink = await worker(pool, channel, now)
    assert len([m for m in sink.messages if m.rule == "predicts_winner"]) == 1


@pytest.mark.parametrize("failure", ["down", "budget", "bad_schema"])
async def test_failed_extraction_degrades_and_ledger_counts_attempts(
    pool: Pool, database_url: str, conn: AsyncConnection, failure: str
) -> None:
    channel, _, now = await seed(pool, database_url)
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "down":
            return httpx.Response(429, headers={"retry-after": "3600"})
        return httpx.Response(200, json=completion({}))

    sink = Sink()
    caps = Caps(month_usd=Decimal("0")) if failure == "budget" else Caps()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await alerting.AlertWorker(
            pool, [channel], GroqLLM(http, Secret("k")), sink, Budget(pool, caps), now=lambda: now
        ).drain()
    assert len(sink.messages) == 3 and all(m.degraded for m in sink.messages)
    expected = 6 if failure == "bad_schema" else 0
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose='extraction'")
    ).fetchone() == (expected,)
    assert calls == (0 if failure == "budget" else 6 if failure == "bad_schema" else 1)


async def test_stale_after_slow_extraction(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, now = await seed(pool, database_url)
    times = [now]

    def respond(req: httpx.Request) -> httpx.Response:
        times[0] = now + timedelta(hours=1)
        return httpx.Response(
            200,
            json=completion(
                {"claim": "new", "summary": "new", "quote": None, "same_as_previous": False}
            ),
        )

    sink = Sink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await alerting.AlertWorker(
            pool,
            [channel],
            GroqLLM(http, Secret("k")),
            sink,
            Budget(pool, Caps()),
            now=lambda: times[0],
        ).drain()
    assert sink.messages == []
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose='extraction'")
    ).fetchone() == (1,)
    assert await (await conn.execute("SELECT DISTINCT outcome FROM alert")).fetchall() == [
        ("stale",)
    ]


async def test_freshness_uses_leg_holding_window_end(pool: Pool, database_url: str) -> None:
    channel, session, now = await seed(pool, database_url, fresh=False)
    candidates = await alerting.pending(pool, [channel])
    first = candidates[0]
    await sessions.end_leg(pool, (await sessions.legs(pool, session))[0], 15_000)
    leg = await sessions.next_leg(pool, sessions.Channel(channel, "twitch", "gorgc"), session)
    # next_leg follows the transcript watermark; explicitly anchor a recorded discontinuity.
    leg = replace(leg, audio_start_ms=15_000)
    await sessions.record_leg(pool, leg, now)
    refreshed = (await alerting.pending(pool, [channel]))[0]
    assert refreshed.said_at == first.said_at
    assert refreshed.said_until == now + timedelta(seconds=15)


@pytest.mark.parametrize(
    "chain,seconds,segment,expected",
    [
        (("seg:1:0:0", 0), 1200, 0, "seg:1:0:0"),
        (("seg:1:0:0", 0), 1201, 0, "seg:1:0:30000"),
        (("seg:1:0:0", 0), 100, 10, "seg:1:10:30000"),
        (None, 100, 0, "seg:1:0:30000"),
    ],
)
def test_coarse_key_chain(
    chain: tuple[str, int] | None, seconds: int, segment: int, expected: str
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    prior = (chain[0], base + timedelta(seconds=chain[1])) if chain else None
    assert (
        alerting.coarse_game_key(
            1, segment, 30_000, base + timedelta(seconds=seconds), prior, timedelta(minutes=20)
        )
        == expected
    )


async def test_pack_key_is_used_when_present_and_cooldown_rows_extend_chain(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, now = await seed(pool, database_url)
    candidates = await alerting.pending(pool, [channel])
    first = candidates[0]
    assert (
        await alerting.game_key(
            pool, replace(first, game_key="league:event:1"), timedelta(minutes=20)
        )
        == "league:event:1"
    )
    key = await alerting.game_key(pool, first, timedelta(minutes=20))
    await alerting.commit(pool, first, alerting.Cooldown(key, 99))
    later = replace(
        first,
        start_ms=1_300_000,
        said_at=first.said_at + timedelta(minutes=19) if first.said_at else now,
    )
    assert await alerting.game_key(pool, later, timedelta(minutes=20)) == key
    assert await alerting.in_cooldown(pool, later) is None
    assert await alerting.previous(pool, later, key) is None


async def test_cooldown_counts_unconfirmed_not_duplicates(pool: Pool, database_url: str) -> None:
    channel, _, now = await seed(pool, database_url)
    candidates = await alerting.pending(pool, [channel])
    first = candidates[0]
    last = next(c for c in candidates if c.rule_id == first.rule_id and c.start_ms > first.start_ms)
    alert = await alerting.commit(
        pool,
        first,
        alerting.Sending(
            "game", {"claim": "same", "summary": "same"}, None, "groq:model", "telegram", ()
        ),
    )
    assert alert
    await alerting.mark_unconfirmed(pool, alert, "timeout")
    assert await alerting.in_cooldown(pool, last) == alert
    assert (await alerting.previous(pool, last, "game")) is not None
    duplicate = await alerting.commit(
        pool,
        last,
        alerting.Duplicate("game", {"claim": "same", "summary": "same"}, alert, "groq:model", ()),
    )
    assert duplicate
    after = replace(last, said_at=now + timedelta(minutes=20))
    assert await alerting.in_cooldown(pool, after) is None
    prior = await alerting.previous(pool, after, "game")
    assert prior is not None and prior.alert_id == alert


async def test_unsure_delivery_becomes_unconfirmed_and_blocked_bot_continues(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, now = await seed(pool, database_url)
    calls = 0
    clock = Clock()

    def bot_response(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("TOKEN", request=req)
        if calls == 2:
            return httpx.Response(403, json={"ok": False, "description": "blocked"})
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": calls, "chat": {"id": 42}}}
        )

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json=completion(
                        {"claim": "new", "summary": "new", "quote": None, "same_as_previous": False}
                    ),
                )
            )
        ) as llm_http,
        httpx.AsyncClient(transport=httpx.MockTransport(bot_response)) as bot_http,
    ):
        bot = TelegramNotifier(bot_http, Secret("TOKEN"), 42, sleep=clock.sleep, clock=clock.now)
        await alerting.AlertWorker(
            pool,
            [channel],
            GroqLLM(llm_http, Secret("k")),
            bot,
            Budget(pool, Caps()),
            now=lambda: now,
        ).drain()
    rows = await (
        await conn.execute("SELECT outcome FROM alert WHERE notifier IS NOT NULL ORDER BY id")
    ).fetchall()
    # A permanent failure leaves Cooldown open, so the next decision for that Rule gets tried.
    assert rows[:3] == [("unconfirmed",), ("failed",), ("sent",)]
    assert rows.count(("unconfirmed",)) == 1
    assert calls == len(rows)


async def test_restart_mid_extraction_reasks_once(
    pool: Pool, database_url: str, conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel, _, now = await seed(pool, database_url)
    original = alerting.commit

    async def cancelled(
        pool: Pool, candidate: alerting.Candidate, decided: alerting.Decided
    ) -> int | None:
        raise asyncio.CancelledError

    monkeypatch.setattr(alerting, "commit", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await worker(pool, channel, now)
    assert await (await conn.execute("SELECT count(*) FROM alert")).fetchone() == (0,)
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose='extraction'")
    ).fetchone() == (0,)
    monkeypatch.setattr(alerting, "commit", original)
    sink = await worker(pool, channel, now)
    assert len(sink.messages) == 3
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose='extraction'")
    ).fetchone() == (3,)


async def test_commit_database_retry_does_not_repeat_paid_call(
    pool: Pool, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    channel, _, now = await seed(pool, database_url)
    original = alerting.commit
    commits = 0

    async def fail_once(
        pool: Pool, candidate: alerting.Candidate, decided: alerting.Decided
    ) -> int | None:
        nonlocal commits
        commits += 1
        if commits == 1:
            raise psycopg.OperationalError("blip")
        return await original(pool, candidate, decided)

    monkeypatch.setattr(alerting, "commit", fail_once)
    sink = await worker(pool, channel, now)
    assert len(sink.messages) == 3 and commits == 13


async def test_jev_and_groq_down_commit_failed_windows(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    clock = Clock()

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=req)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(down)) as jev_http,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(429, headers={"retry-after": "3600"})
            )
        ) as llm_http,
    ):
        gate = FallbackGate(
            JevGate(jev_http, Secret("k"), give_up_s=0, clock=clock.now),
            LLMGate(GroqLLM(llm_http, Secret("k"))),
            clock=clock.now,
        )
        await seed(pool, database_url, gate=gate)
    rows = await (await conn.execute("SELECT status, reason FROM gate_window")).fetchall()
    assert len(rows) == 8
    assert all(
        status == "failed"
        and "jev unavailable" in reason
        and "fallback: groq unavailable" in reason
        for status, reason in rows
    )
    assert await (
        await conn.execute("SELECT max(audio_end_ms) FROM transcript_span")
    ).fetchone() == (240_000,)


async def test_worker_bug_marks_failed_and_moves_on(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, now = await seed(pool, database_url)

    class BugOnce(Sink):
        async def send(self, message: Any, *, give_up_s: float) -> Delivery:
            if not self.messages:
                self.messages.append(message)
                raise ValueError("TOKEN-bearing diagnostic must not be logged")
            return await super().send(message, give_up_s=give_up_s)

    sink = BugOnce()
    async with httpx.AsyncClient(transport=replay_transport(EXTRACTION, arm="default")) as http:
        await alerting.AlertWorker(
            pool, [channel], GroqLLM(http, Secret("k")), sink, Budget(pool, Caps()), now=lambda: now
        ).drain()
    assert len(sink.messages) == 3
    assert await (
        await conn.execute("SELECT outcome FROM alert ORDER BY id LIMIT 3")
    ).fetchall() == [("unconfirmed",), ("sent",), ("sent",)]


async def test_second_process_does_not_poll_and_survivor_takes_over(
    pool: Pool, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiroku.db import connect

    class Poller(Sink):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def taps(self) -> AsyncGenerator[Tap]:
            self.started.set()
            await asyncio.Event().wait()
            if False:
                yield Tap(1, True, None, Delivery("42:1"), datetime.now(UTC))

    notifier = Poller()
    real_sleep = asyncio.sleep

    async def quick_sleep(seconds: float) -> None:
        await real_sleep(0.01)

    monkeypatch.setattr(labels.asyncio, "sleep", quick_sleep)
    async with connect(database_url) as owner:
        await owner.execute("SELECT pg_advisory_lock(%s,0)", [labels.TELEGRAM_LOCK_CLASS])
        task = asyncio.create_task(labels.run_taps(pool, database_url, notifier))
        await real_sleep(0.05)
        assert not notifier.started.is_set()
    await asyncio.wait_for(notifier.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_llm_estimate_covers_recorded_usage() -> None:
    from kiroku.llm.groq import PRICE, estimate_tokens

    def records() -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for path in (EXTRACTION, FALLBACK)
            for line in Path(path).read_text().splitlines()
        ]

    rows = await asyncio.to_thread(records)
    checked = 0
    for row in rows:
        if "request" not in row or row.get("status", 200) != 200:
            continue
        usage = row["response"]["usage"]
        actual = Tokens(usage["prompt_tokens"], usage["completion_tokens"])
        estimate = estimate_tokens(row["request"], row["request"]["max_completion_tokens"])
        assert PRICE.usd(estimate) >= PRICE.usd(actual)
        checked += 1
    assert checked >= 69


async def test_fallback_auth_error_writes_costs_and_keeps_gating(
    pool: Pool, database_url: str, conn: AsyncConnection, caplog: pytest.LogCaptureFixture
) -> None:
    from kiroku.gate import GateAnswer, GateRequest, GateUnavailable

    cost = CostEntry("jev", "jev-1.13.0", Purpose.GATE, Tokens(100), Decimal(".000005"), 0)

    class LostPrimary:
        name = "jev"

        async def evaluate(self, request: GateRequest) -> GateAnswer:
            raise GateUnavailable("jev unavailable", (cost,))

    attempts = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json=completion({})) if attempts == 1 else httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await seed(
            pool,
            database_url,
            gate=FallbackGate(LostPrimary(), LLMGate(GroqLLM(http, Secret("k")))),
        )
    assert await (
        await conn.execute(
            "SELECT provider,count(*) FROM cost_ledger GROUP BY provider ORDER BY provider"
        )
    ).fetchall() == [("groq", 1), ("jev", 8)]
    assert await (
        await conn.execute("SELECT count(*) FROM gate_window WHERE status='failed'")
    ).fetchone() == (8,)
    assert attempts == 2
    assert sum(r.levelno == logging.ERROR for r in caplog.records) == 1


async def test_worker_bug_before_send_marks_failed_and_moves_on(
    pool: Pool, database_url: str, conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiroku import extraction

    original = extraction.extract
    attempts = 0

    async def faulty(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("provider bug")
        return await original(*args, **kwargs)

    monkeypatch.setattr(extraction, "extract", faulty)
    channel, _, now = await seed(pool, database_url)
    sink = Sink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json=completion(
                    {"claim": "new", "summary": "new", "quote": None, "same_as_previous": False}
                ),
            )
        )
    ) as http:
        await alerting.AlertWorker(
            pool, [channel], GroqLLM(http, Secret("k")), sink, Budget(pool, Caps()), now=lambda: now
        ).drain()
    assert len(sink.messages) == 3
    assert await (
        await conn.execute("SELECT outcome FROM alert ORDER BY id LIMIT 1")
    ).fetchone() == ("failed",)


async def test_fallback_budget_costs_reach_the_window_transaction(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    from kiroku.budget import OverBudget, Refusal
    from kiroku.gate import GateAnswer, GateRequest

    cost = CostEntry("jev", "jev-1.13.0", Purpose.GATE, Tokens(100), Decimal(".000005"), 0)

    class Refused:
        name = "fallback"

        async def evaluate(self, request: GateRequest) -> GateAnswer:
            raise OverBudget(Refusal("month", Decimal("0"), Decimal("0"), Decimal(".001")), (cost,))

    await seed(pool, database_url, gate=Refused())
    assert await (
        await conn.execute("SELECT count(*) FROM gate_window WHERE status='over_budget'")
    ).fetchone() == (8,)
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE provider='jev'")
    ).fetchone() == (8,)


async def test_auth_cost_commit_lost_ack_is_idempotent(
    pool: Pool, database_url: str, conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    from kiroku.gate import GateAnswer, GateAuthError, GateRequest

    cost = CostEntry("jev", "jev-1.13.0", Purpose.GATE, Tokens(100), Decimal(".000005"), 0)

    class Auth:
        name = "jev"

        async def evaluate(self, request: GateRequest) -> GateAnswer:
            raise GateAuthError("wrong key after lost response", (cost,))

    from kiroku import ledger

    original = ledger.retrying
    commits = 0

    async def lose_ack(what: str, fn: Any) -> Any:
        async def write() -> Any:
            nonlocal commits
            commits += 1
            result = await fn()
            if commits == 1:
                raise psycopg.OperationalError("commit acknowledgement lost")
            return result

        return await original(what, write)

    monkeypatch.setattr(ledger, "retrying", lose_ack)
    channel, session, _ = await seed(pool, database_url, gate=Auth())
    assert commits == 2
    assert await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone() == (1,)
    assert await (await conn.execute("SELECT count(*) FROM gate_window")).fetchone() == (0,)
    assert await (await conn.execute("SELECT DISTINCT session_id FROM cost_ledger")).fetchone() == (
        session,
    )
    # A new process with a corrected primary key must see and gate the original backlog.
    from kiroku.budget import Unlimited
    from kiroku.gate.windows import WindowPolicy
    from kiroku.gating import GateLoop
    from tests.test_gating import FakeGate

    restarted = GateLoop(
        pool,
        session,
        "recorded",
        FakeGate(0.8),
        Unlimited(),
        lambda: {},
        policy=WindowPolicy(length_ms=30_000),
    )
    restarted.finish(session_ended=True)
    await restarted.run()
    assert len(await alerting.pending(pool, [channel])) == 20
    assert await (await conn.execute("SELECT count(*) FROM gate_window")).fetchone() == (8,)


async def test_two_rules_in_one_window_send_separate_buttons(pool: Pool, database_url: str) -> None:
    channel, _, now = await seed(pool, database_url)
    api = BotAPI()
    clock = Clock()
    async with (
        httpx.AsyncClient(transport=replay_transport(EXTRACTION, arm="default")) as llm_http,
        httpx.AsyncClient(transport=httpx.MockTransport(api)) as bot_http,
    ):
        notifier = TelegramNotifier(
            bot_http, Secret("TOKEN"), 42, sleep=clock.sleep, clock=clock.now
        )
        await alerting.AlertWorker(
            pool,
            [channel],
            GroqLLM(llm_http, Secret("k")),
            notifier,
            Budget(pool, Caps()),
            now=lambda: now,
        ).drain()
    assert len(api.sent) == 3
    buttons = [body["reply_markup"]["inline_keyboard"][0] for body in api.sent]
    ids = [int(row[0]["callback_data"].split(":")[1]) for row in buttons]
    assert len(set(ids)) == 3
    assert all(
        row[0]["callback_data"] == f"l:{alert_id}:y"
        and row[1]["callback_data"] == f"l:{alert_id}:n"
        for row, alert_id in zip(buttons, ids, strict=True)
    )
    assert "predicts_winner" in api.sent[0]["text"] and "talks_item" in api.sent[1]["text"]


async def test_pack_key_is_persisted_by_the_worker(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    from kiroku import gating
    from kiroku.budget import Unlimited
    from kiroku.gate import GateRequest, RuleQuestion
    from kiroku.gate.windows import Bounds
    from tests.test_gating import FakeGate

    channel, session, now = await seed(pool, database_url)
    candidates = await alerting.pending(pool, [channel])
    first = candidates[0]
    for candidate in candidates:
        await alerting.commit(pool, candidate, alerting.Stale())
    question = RuleQuestion(first.rule_id, first.rule, first.question, Decimal(".6"))
    answer = await FakeGate(0.9).evaluate(GateRequest(first.state, (question,), Unlimited()))
    await gating.commit(
        pool,
        session,
        "recorded",
        Bounds(240_000, 270_000),
        first.state,
        (question,),
        gating.Answered(answer),
        game_key="league:event:1",
    )
    sink = Sink()
    async with httpx.AsyncClient(transport=replay_transport(EXTRACTION, arm="default")) as http:
        await alerting.AlertWorker(
            pool, [channel], GroqLLM(http, Secret("k")), sink, Budget(pool, Caps()), now=lambda: now
        ).drain()
    assert len(sink.messages) == 1
    assert await (
        await conn.execute("SELECT game_key, outcome FROM alert WHERE game_key IS NOT NULL")
    ).fetchall() == [("league:event:1", "sent")]


@pytest.mark.parametrize("left", [0.001, 5.5])
async def test_near_deadline_commits_stale_without_cooldown(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
    left: float,
) -> None:
    channel, _, now = await seed(pool, database_url)
    first = (await alerting.pending(pool, [channel]))[0]
    assert first.said_until is not None
    await conn.execute("DELETE FROM gate_decision WHERE id <> %s", [first.decision_id])
    clock = Clock()
    api = BotAPI()
    receipt = first.said_until + timedelta(minutes=10) - timedelta(seconds=left)
    async with (
        httpx.AsyncClient(transport=replay_transport(EXTRACTION, arm="default")) as llm_http,
        httpx.AsyncClient(transport=httpx.MockTransport(api)) as bot_http,
    ):
        bot = TelegramNotifier(bot_http, Secret("TOKEN"), 42, clock=clock.now, sleep=clock.sleep)
        if left > 5:
            from tests.test_alerts import message

            await bot.send(message(), give_up_s=20)
            api.sent.clear()
        worker = alerting.AlertWorker(
            pool,
            [channel],
            GroqLLM(llm_http, Secret("k")),
            bot,
            Budget(pool, Caps()),
            now=lambda: receipt,
        )
        await worker.drain()
    assert not api.sent
    assert await (await conn.execute("SELECT outcome FROM alert")).fetchall() == [("stale",)]
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose = 'extraction'")
    ).fetchone() == (1,)
    assert await alerting.in_cooldown(pool, replace(first, said_at=now)) is None


@pytest.mark.parametrize("date", [0, None])
async def test_inaccessible_callback_confirms_with_receipt_time(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
    date: int | None,
) -> None:
    from kiroku.notify.telegram import parse_tap

    channel, _, now = await seed(pool, database_url)
    first = (await alerting.pending(pool, [channel]))[0]
    alert_id = await alerting.commit(
        pool,
        first,
        alerting.Sending(
            "game",
            {"summary": "prediction"},
            None,
            None,
            "telegram",
            (),
        ),
    )
    assert alert_id is not None
    await alerting.mark_unconfirmed(pool, alert_id, "timeout")
    message: dict[str, Any] = {"chat": {"id": 42}, "message_id": 7}
    if date is not None:
        message["date"] = date
    tap = parse_tap(
        {"callback_query": {"id": "q", "data": f"l:{alert_id}:y", "message": message}},
        42,
        now=lambda: now,
    )
    assert tap is not None and await labels.record(pool, tap)
    assert await (await conn.execute("SELECT outcome, sent_at FROM alert")).fetchone() == (
        "sent",
        now,
    )
    assert await (await conn.execute("SELECT applies FROM label")).fetchone() == (True,)


async def test_pending_reads_whole_batch_in_one_checkout(
    pool: Pool,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _, _ = await seed(pool, database_url)
    checkouts = 0
    original = pool.connection

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal checkouts
        checkouts += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pool, "connection", counted)
    candidates = await alerting.pending(pool, [channel])
    assert len(candidates) == 12 and all(c.said_until is not None for c in candidates)
    assert checkouts == 1


async def test_bug_outcome_never_enters_sending(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _, now = await seed(pool, database_url)
    first = (await alerting.pending(pool, [channel]))[0]
    from kiroku import extraction

    await conn.execute("DELETE FROM gate_decision WHERE id <> %s", [first.decision_id])

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("provider bug")

    monkeypatch.setattr(extraction, "extract", broken)
    # An INSERT of sending would fail before _bug could perform its second write.
    await conn.execute("""
        CREATE FUNCTION reject_sending() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.outcome = 'sending' THEN RAISE EXCEPTION 'unexpected sending'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER reject_sending BEFORE INSERT ON alert
        FOR EACH ROW EXECUTE FUNCTION reject_sending();
    """)
    sink = Sink()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    ) as http:
        worker = alerting.AlertWorker(
            pool,
            [channel],
            GroqLLM(http, Secret("k")),
            sink,
            Budget(pool, Caps()),
            now=lambda: now,
        )
        await worker.drain()
    assert await (await conn.execute("SELECT outcome, reason FROM alert")).fetchone() == (
        "failed",
        "bug: ValueError",
    )
    assert await alerting.in_cooldown(pool, first) is None
    assert await alerting.recover(pool, [channel]) == 0
    assert sink.messages == []


async def test_budget_db_blip_after_paid_extraction_keeps_decision_pending_and_costs(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    channel, _, now = await seed(pool, database_url)
    original = Budget.check
    checks = 0

    async def flaky(self: Budget, session: int | None, estimate: Decimal) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise psycopg.OperationalError("budget DB blip")
        await original(self, session, estimate)

    monkeypatch.setattr(Budget, "check", flaky)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=completion({})))
    ) as http:
        sink = Sink()
        w = alerting.AlertWorker(
            pool, [channel], GroqLLM(http, Secret("k")), sink, Budget(pool, Caps()), now=lambda: now
        )
        with pytest.raises(psycopg.OperationalError):
            await w.drain()
    assert sink.messages == []
    assert await (await conn.execute("SELECT count(*) FROM alert")).fetchone() == (0,)
    assert await (
        await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose='extraction'")
    ).fetchone() == (1,)
