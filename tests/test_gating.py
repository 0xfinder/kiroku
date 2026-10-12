"""Gate decisions in Postgres: one row per (Window, Rule), whatever happens."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, LiteralString, cast

import httpx
import psycopg
import pytest
from psycopg import AsyncConnection

from kiroku import rules, sessions, transcript
from kiroku.budget import Budget, Caps, OverBudget, Refusal, Unlimited
from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.gate import GateAnswer, GateAuthError, GateFailed, GateRequest, Mode
from kiroku.gate.jev import JevGate
from kiroku.gate.windows import WindowPolicy
from kiroku.gating import GateLoop
from kiroku.ledger import CostEntry, Purpose, Tokens
from kiroku.sessions import Channel, claim_channels
from kiroku.source import StreamMeta
from kiroku.transcriber import Result, Utterance

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
META = StreamMeta("broadcast-a", "Blast Slam co-stream", "Dota 2")
NAME = "groq:whisper-large-v3-turbo"
POLICY = WindowPolicy(length_ms=45_000)


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


@pytest.fixture
async def channel(database_url: str) -> AsyncGenerator[Channel]:
    async with claim_channels(database_url, "twitch", ["gorgc"]) as channels:
        yield channels[0]


@pytest.fixture
async def session(pool: Pool, channel: Channel) -> int:
    session_id = await sessions.open_session(pool, channel, META, T0)
    leg = await sessions.next_leg(pool, channel, session_id)
    await sessions.record_leg(pool, leg, T0)
    return session_id


type Item = float | BaseException


class FakeGate:
    """Answers every Rule with the next item's probability, or raises it."""

    name = "fake:gate"

    def __init__(self, *items: Item) -> None:
        self.items = list(items)
        self.requests: list[GateRequest] = []
        self.block: asyncio.Event | None = None
        self._asked = asyncio.Condition()

    async def wait_for_requests(self, n: int) -> None:
        async with asyncio.timeout(10), self._asked:
            await self._asked.wait_for(lambda: len(self.requests) >= n)

    async def evaluate(self, request: GateRequest) -> GateAnswer:
        self.requests.append(request)
        async with self._asked:
            self._asked.notify_all()
        if self.block is not None:
            await self.block.wait()
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        cost = CostEntry("fake", "1", Purpose.GATE, Tokens(100), Decimal("0.000005"), 1)
        probs = {q.rule_id: item for q in request.rules}
        return GateAnswer("fake:1", probs, Mode.WATCHING_PRO, 0.9, (cost,))


async def speak(pool: Pool, session: int, start_s: int, end_s: int, *texts: str) -> None:
    """Commit one span [start_s, end_s) holding `texts` as utterances, spread across it."""
    step = (end_s - start_s) * 1000 // max(len(texts), 1)
    utterances = tuple(
        Utterance(start_s * 1000 + i * step, start_s * 1000 + (i + 1) * step, t)
        for i, t in enumerate(texts)
    )
    result = Result(start_s * 1000, end_s * 1000, utterances, None)
    await transcript.commit(pool, session, NAME, result)


def loop(pool: Pool, session: int, gate: Any, guard: Any = None) -> GateLoop:
    return GateLoop(
        pool,
        session,
        NAME,
        gate,
        guard or Unlimited(),
        lambda: {"title": META.title},
        policy=POLICY,
        poll_s=0.05,
    )


async def gate_once(pool: Pool, session: int, gate: Any, *, ended: bool, guard: Any = None) -> None:
    g = loop(pool, session, gate, guard)
    g.finish(session_ended=ended)
    async with asyncio.timeout(10):
        await g.run()


async def rows(conn: AsyncConnection, query: str) -> list[tuple[Any, ...]]:
    cur = await conn.execute(cast(LiteralString, query))
    return list(await cur.fetchall())


async def two_rules(pool: Pool) -> Sequence[rules.Rule]:
    return [
        await rules.add(
            pool, "predicts_winner", "Does he predict a winner?", threshold=Decimal("0.6")
        ),
        await rules.add(pool, "talks_item", "Does he discuss an item?", threshold=Decimal("0.7")),
    ]


async def test_every_window_rule_pair_has_exactly_one_decision(
    pool: Pool, session: int, conn: AsyncConnection
) -> None:
    await two_rules(pool)
    await speak(pool, session, 0, 45, "Spirit wins this, 70-30.")
    await speak(pool, session, 45, 90)  # silence
    await speak(pool, session, 90, 135, "He buys a BKB.")
    await speak(pool, session, 135, 180, "Butterfly soon.")
    await speak(pool, session, 180, 200, "The end.")
    refused = OverBudget(Refusal("session", Decimal("1.50"), Decimal("1.50"), Decimal("0.00003")))
    gate = FakeGate(0.6, refused, GateFailed("jev unavailable after 6 attempts: HTTP 529"), 0.1)
    await gate_once(pool, session, gate, ended=True)

    windows = await rows(
        conn,
        "SELECT start_ms, end_ms, status, reason IS NOT NULL, mode FROM gate_window ORDER BY 1",
    )
    assert windows == [
        (0, 45_000, "answered", False, "watching_pro"),
        (45_000, 90_000, "empty", False, None),  # silence: no paid call
        (90_000, 135_000, "over_budget", True, None),
        (135_000, 180_000, "failed", True, None),
        (180_000, 200_000, "answered", False, "watching_pro"),  # the session's partial end
    ]
    assert len(gate.requests) == 4
    decisions = await rows(
        conn,
        """
        SELECT w.start_ms, r.name, d.probability, d.passed FROM gate_decision d
        JOIN gate_window w ON w.id = d.window_id JOIN rule r ON r.id = d.rule_id
        ORDER BY w.start_ms, r.id
        """,
    )
    assert len(decisions) == 5 * 2
    # Exactly at the threshold passes (0.60 >= 0.6); NULL never passes.
    assert decisions[0] == (0, "predicts_winner", Decimal("0.6000"), True)
    assert decisions[1] == (0, "talks_item", Decimal("0.6000"), False)
    assert {d[3] for d in decisions[2:8]} == {False}
    assert {d[2] for d in decisions[2:8]} == {None}
    # One ledger row per answered call, under the session.
    assert await rows(
        conn, "SELECT count(*), sum(usd) FROM cost_ledger WHERE purpose = 'gate'"
    ) == [(2, Decimal("0.000010"))]
    # The state the Gate saw is stored: earlier speech is context, not new.
    state = await rows(conn, "SELECT state FROM gate_window WHERE start_ms = 135000")
    assert state[0][0]["new"] == "Butterfly soon."
    assert state[0][0]["context"]["earlier"] == "He buys a BKB."


async def test_restart_resumes_without_regating_or_holes(
    pool: Pool, session: int, conn: AsyncConnection
) -> None:
    await two_rules(pool)
    for start in range(0, 135, 45):
        await speak(pool, session, start, start + 45, f"talk at {start}")
    gate = FakeGate(0.5)
    gate.block = asyncio.Event()
    first = asyncio.create_task(loop(pool, session, gate).run())
    await gate.wait_for_requests(1)
    gate.block.set()
    await gate.wait_for_requests(2)
    # Crash in the middle of the third Window: its answer is lost, never committed.
    gate.block.clear()
    await gate.wait_for_requests(3)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    gate.block.set()
    await gate_once(pool, session, gate, ended=False)
    assert await rows(conn, "SELECT start_ms FROM gate_window ORDER BY 1") == [
        (0,),
        (45_000,),
        (90_000,),
    ]
    assert await rows(conn, "SELECT count(*) FROM gate_decision") == [(6,)]


async def test_shutdown_leaves_the_partial_window_for_the_restart(
    pool: Pool, session: int, conn: AsyncConnection
) -> None:
    await two_rules(pool)
    await speak(pool, session, 0, 63, "first", "second")
    await gate_once(pool, session, FakeGate(0.2), ended=False)
    assert await rows(conn, "SELECT start_ms, end_ms FROM gate_window") == [(0, 45_000)]
    # After the restart the transcript grows, and the Window closes at full length.
    await speak(pool, session, 63, 95, "third")
    await gate_once(pool, session, FakeGate(0.2), ended=False)
    assert await rows(conn, "SELECT start_ms, end_ms FROM gate_window ORDER BY 1") == [
        (0, 45_000),
        (45_000, 90_000),
    ]


async def test_rule_changes_apply_from_the_next_window(
    pool: Pool, session: int, conn: AsyncConnection
) -> None:
    winner, item = await two_rules(pool)
    await speak(pool, session, 0, 45, "one")
    await gate_once(pool, session, FakeGate(0.5), ended=False)
    await conn.execute("UPDATE rule SET enabled = false WHERE id = %s", [item.id])
    await conn.execute("UPDATE rule SET instructions = 'Edited?' WHERE id = %s", [winner.id])
    await rules.add(pool, "new_rule", "Is it new?", threshold=Decimal("0.5"))
    await speak(pool, session, 45, 90, "two")
    await gate_once(pool, session, FakeGate(0.5), ended=False)
    got = await rows(
        conn,
        """
        SELECT w.start_ms, r.name, d.question LIKE 'In `new`: Edited?%%' FROM gate_decision d
        JOIN gate_window w ON w.id = d.window_id JOIN rule r ON r.id = d.rule_id
        ORDER BY w.start_ms, r.id
        """,
    )
    assert got == [
        (0, "predicts_winner", False),
        (0, "talks_item", False),
        (45_000, "predicts_winner", True),  # the edit, recorded as asked
        (45_000, "new_rule", False),
    ]


async def test_a_cap_below_one_call_skips_the_call_and_logs_why(
    pool: Pool, session: int, conn: AsyncConnection, caplog: pytest.LogCaptureFixture
) -> None:
    await two_rules(pool)
    await speak(pool, session, 0, 45, "I think Spirit wins.")
    sent: list[httpx.Request] = []

    def jev(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500)

    budget = Budget(pool, Caps(session_usd=Decimal("0.00001"), month_usd=Decimal("20")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(jev)) as http:
        caplog.set_level(logging.INFO)
        gate = JevGate(http, Secret("k"))
        await gate_once(pool, session, gate, ended=True, guard=budget.guard(session))

    assert sent == []
    ((status, reason),) = await rows(conn, "SELECT status, reason FROM gate_window")
    assert status == "over_budget"
    assert reason.startswith("session Budget cap $0.00001 reached: spent $0, next call ~$0.0000")
    assert await rows(conn, "SELECT probability, passed FROM gate_decision") == [(None, False)] * 2
    assert await rows(conn, "SELECT count(*) FROM cost_ledger") == [(0,)]
    assert any(
        r.levelno == logging.WARNING and "session Budget cap" in r.getMessage()
        for r in caplog.records
    )


async def test_the_budget_counts_other_sessions_toward_the_month_only(
    pool: Pool, channel: Channel, session: int
) -> None:
    other = await sessions.open_session(
        pool, channel, StreamMeta("broadcast-b", "t", None), datetime.now(UTC)
    )
    async with pool.connection() as c:
        from kiroku import ledger

        cost = CostEntry("groq", "w", Purpose.TRANSCRIBE, Tokens(1), Decimal("1.00"), 1, other)
        await ledger.record(c, cost)
    budget = Budget(pool, Caps(session_usd=Decimal("0.50"), month_usd=Decimal("1.20")))
    await budget.guard(session).check(Decimal("0.10"))  # month 1.10 <= 1.20, session 0.10
    with pytest.raises(OverBudget, match="month"):
        await budget.guard(session).check(Decimal("0.30"))
    with pytest.raises(OverBudget, match="session"):
        await budget.guard(other).check(Decimal("0.10"))
    # Last month's spend doesn't count this month.
    later = Budget(
        pool, Caps(month_usd=Decimal("0.5")), now=lambda: datetime(2099, 1, 5, tzinfo=UTC)
    )
    await later.guard(None).check(Decimal("0.4"))


async def test_a_gap_is_recorded_and_doesnt_block_later_windows(
    pool: Pool, session: int, conn: AsyncConnection
) -> None:
    await two_rules(pool)
    await speak(pool, session, 0, 20, "before the crash")
    await speak(pool, session, 30, 90, "after the crash")  # 20-30 s never transcribed
    await gate_once(pool, session, FakeGate(0.3), ended=False)
    assert await rows(conn, "SELECT start_ms, covered_ms FROM gate_window ORDER BY 1") == [
        (0, 35_000),
        (45_000, 45_000),
    ]


async def test_a_wrong_key_stops_the_gate_but_writes_nothing(
    pool: Pool, session: int, conn: AsyncConnection, caplog: pytest.LogCaptureFixture
) -> None:
    await two_rules(pool)
    await speak(pool, session, 0, 90, "one", "two")
    gate = FakeGate(GateAuthError("Jev rejected the API key (HTTP 401)"))
    async with asyncio.timeout(10):
        await loop(pool, session, gate).run()  # returns by itself
    assert len(gate.requests) == 1
    assert await rows(conn, "SELECT count(*) FROM gate_window") == [(0,)]
    assert "the Gate is off for this session" in caplog.text
    # With the key fixed, a restart gates the backlog from the start.
    await gate_once(pool, session, FakeGate(0.3), ended=False)
    assert await rows(conn, "SELECT count(*) FROM gate_window") == [(2,)]


async def test_a_rule_cannot_name_state_fields(pool: Pool) -> None:
    # Only the Gate's wrapper points Jev at `new` and `context`.
    with pytest.raises(rules.RuleError, match="backticks"):
        await rules.add(pool, "sneaky", "Is it said in `context`?", threshold=Decimal("0.5"))
    async with pool.connection() as conn:  # and the table refuses it too
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "INSERT INTO rule (name, instructions, threshold) VALUES ('x', '`new`?', 0.5)"
            )


async def test_a_disabled_rule_is_not_asked(
    pool: Pool, session: int, conn: AsyncConnection
) -> None:
    winner, _ = await two_rules(pool)
    await rules.set_enabled(pool, winner.name, False)
    await speak(pool, session, 0, 45, "Spirit wins.")
    gate = FakeGate(0.9)
    await gate_once(pool, session, gate, ended=False)
    assert [q.name for q in gate.requests[0].rules] == ["talks_item"]
    assert await rows(conn, "SELECT count(*) FROM gate_decision") == [(1,)]


async def test_a_gate_bug_is_logged_and_never_raised(
    pool: Pool, session: int, caplog: pytest.LogCaptureFixture
) -> None:
    await two_rules(pool)
    await speak(pool, session, 0, 45, "one")
    async with asyncio.timeout(10):
        await loop(pool, session, FakeGate(RuntimeError("boom"))).run()  # returns, no raise
    assert "Gate crashed" in caplog.text and "boom" in caplog.text


async def test_the_last_pass_survives_a_database_blip(
    pool: Pool, session: int, conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiroku.gating

    await two_rules(pool)
    await speak(pool, session, 0, 20, "the end")
    real, calls = kiroku.gating.progress, 0

    async def flaky(*args: Any) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise psycopg.OperationalError("server closed the connection")
        return await real(*args)

    monkeypatch.setattr(kiroku.gating, "progress", flaky)
    await gate_once(pool, session, FakeGate(0.4), ended=True)
    assert await rows(conn, "SELECT start_ms, end_ms FROM gate_window") == [(0, 20_000)]


async def test_sessions_that_closed_ungated_get_their_last_windows(
    pool: Pool, channel: Channel, session: int, conn: AsyncConnection, tmp_path: Any
) -> None:
    from kiroku.source import NotLive
    from kiroku.watch import watch

    await two_rules(pool)
    # The stream ran 70 s and ended while kiroku was down: nothing was gated.
    await speak(pool, session, 0, 70, "first", "second", "third")

    class Offline:
        platform, login = "twitch", "gorgc"

        async def probe(self) -> NotLive:
            return NotLive()

        def capture(self, archive: Any) -> Any:
            raise AssertionError("an offline channel is never captured")

    class Transcriber:
        name = NAME

        def transcribe(self, audio: Any, context: Any) -> Any:
            raise AssertionError("nothing to transcribe")

    gate = FakeGate(0.4)
    task = asyncio.create_task(
        watch(
            pool,
            [(channel, Offline())],
            Transcriber(),
            tmp_path,
            budget=Budget(pool, Caps()),
            gate=gate,
            poll_s=0.05,
        )
    )
    await gate.wait_for_requests(2)
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert await rows(conn, "SELECT start_ms, end_ms FROM gate_window ORDER BY 1") == [
        (0, 45_000),
        (45_000, 70_000),
    ]
    assert await rows(conn, "SELECT count(*) FROM stream_session WHERE ended_at IS NULL") == [(0,)]
