"""Notes acceptance: durable timeline behavior through the real provider and Postgres."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from psycopg import AsyncConnection
from typer.testing import CliRunner

from kiroku import notes
from kiroku.budget import Budget, Caps
from kiroku.cli import app
from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.gate.windows import Bounds
from kiroku.ledger import Purpose
from kiroku.llm import LLMRequest, LLMResult
from kiroku.llm.groq import GroqLLM
from kiroku.notes_schema import GENERAL
from kiroku.packs.league import LeaguePack
from kiroku.packs.league.data import PUBLIC_WEBSITE_KEY
from kiroku.packs.league.notes import SCHEMAS
from tests.fakes.alerts import replay_transport
from tests.fakes.notes import TRANSCRIBER, seed
from tests.test_alerts import Clock, completion

CASSETTE = "spikes/results/2026-10-13-notes.jsonl"


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


def read_recording() -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(CASSETTE).read_text().splitlines()]


def good(at: int = 0) -> dict[str, Any]:
    return {"topics": [{"at_ms": at, "topic": "match"}], "takes": [], "summary": "match"}


def responder(request: httpx.Request) -> httpx.Response:
    user = json.loads(json.loads(request.content)["messages"][1]["content"])
    return httpx.Response(200, json=completion(good(user["turns"][0]["at_ms"])))


def worker(pool: Pool, channel: int, http: httpx.AsyncClient, **kwargs: Any) -> notes.NotesWorker:
    return notes.NotesWorker(
        pool, [channel], TRANSCRIBER, GroqLLM(http, Secret("k")), Budget(pool, Caps()), **kwargs
    )


@pytest.mark.parametrize("repeat", [0, 1, 2])
@pytest.mark.parametrize("fixture", ["league", "general"])
async def test_live_fixture_replay(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
    repeat: int,
    fixture: Literal["league", "general"],
) -> None:
    channel, session, now = await seed(pool, database_url, fixture)
    async with httpx.AsyncClient(transport=replay_transport(CASSETTE, repeat=repeat)) as http:
        pack = LeaguePack(http, Path("/tmp/unused-notes-pack"), PUBLIC_WEBSITE_KEY)
        w = worker(pool, channel, http, packs=[pack], now=lambda: now)
        await w.drain()
        # A new process starts from the durable terminal span end, with no further calls.
        await worker(pool, channel, http, packs=[pack], now=lambda: now).drain()
    rows = await notes.rows(pool, session_id=session)
    assert len(rows) == (4 if fixture == "league" else 2)
    assert all(r["status"] == "ok" for r in rows)
    for row in rows:
        schema = SCHEMAS["watching_pro"] if fixture == "league" else GENERAL
        schema.parse(row["body"], Bounds(row["start_ms"], row["end_ms"]))
    recorded = await asyncio.to_thread(read_recording)
    expected = next(
        r["result"]
        for r in recorded
        if "result" in r and r["result"]["fixture"] == fixture and r["result"]["repeat"] == repeat
    )
    assert [r["body"] for r in rows] == [r["body"] for r in expected["notes"]]
    assert await (
        await conn.execute(
            "SELECT count(*),sum(usd) FROM cost_ledger WHERE purpose='notes'",
        )
    ).fetchone() == (expected["ledger"][0], Decimal(expected["ledger"][1]))


@pytest.mark.parametrize("bad", [{}, good(-1), good(180_000), good(True)])
async def test_invalid_retries_once_stores_both_outputs_and_continues(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
    bad: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, _, _ = await seed(pool, database_url, gate=False)
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=completion(bad)) if calls <= 2 else responder(req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await worker(pool, channel, http, gated=False).drain()
    rows = await notes.rows(pool)
    assert [r["status"] for r in rows] == ["invalid", "ok"]
    assert len(rows[0]["outputs"]) == 2 and all(a["error"] for a in rows[0]["outputs"])
    assert calls == 3
    assert any(r.levelno == logging.WARNING and "invalid" in r.message for r in caplog.records)
    assert await (
        await conn.execute(
            "SELECT count(*) FROM cost_ledger WHERE purpose='notes'",
        )
    ).fetchone() == (3,)


async def test_commit_is_atomic_and_repeated_commit_is_noop(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
) -> None:
    channel, _, now = await seed(pool, database_url)
    span = (
        await notes.pending(pool, [channel], TRANSCRIBER, notes.NotesPolicy(), now, gated=True)
    )[0]
    prepared = await notes.prepare(pool, span, TRANSCRIBER, {}, gated=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        await worker(pool, channel, http).drain()
    before = await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone()
    assert not await notes.commit(pool, prepared, TRANSCRIBER, notes.Outcome("empty"))
    assert await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone() == before
    assert await (await conn.execute("SELECT count(*) FROM note")).fetchone() == (2,)


async def test_empty_spans_no_calls(pool: Pool, database_url: str) -> None:
    channel, _, _ = await seed(pool, database_url, speech=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: pytest.fail("paid call"))
    ) as http:
        await worker(pool, channel, http).drain()
    assert [r["status"] for r in await notes.rows(pool)] == ["empty", "empty"]


async def test_unavailable_then_expiry(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, _, now = await seed(pool, database_url)
    clock = Clock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    ) as http:
        llm = GroqLLM(http, Secret("k"), clock=clock.now, sleep=clock.sleep)
        w = notes.NotesWorker(
            pool, [channel], TRANSCRIBER, llm, Budget(pool, Caps()), now=lambda: now
        )
        await w.drain()
        assert await notes.rows(pool) == []
        await notes.NotesWorker(
            pool,
            [channel],
            TRANSCRIBER,
            llm,
            Budget(pool, Caps()),
            now=lambda: now + timedelta(minutes=61),
        ).drain()
    assert [r["status"] for r in await notes.rows(pool)] == ["failed", "failed"]
    assert await (await conn.execute("SELECT count(*) FROM note_retry")).fetchone() == (0,)


async def test_pending_paid_attempt_survives_restart(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
) -> None:
    channel, _, now = await seed(pool, database_url)
    clock = Clock()
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(200, json=completion({}))
            if calls == 1
            else httpx.Response(429, headers={"retry-after": "60"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await notes.NotesWorker(
            pool,
            [channel],
            TRANSCRIBER,
            GroqLLM(http, Secret("k"), clock=clock.now, sleep=clock.sleep),
            Budget(pool, Caps()),
            now=lambda: now,
        ).drain()
    assert not await notes.rows(pool)
    assert await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone() == (1,)
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        await worker(pool, channel, http, now=lambda: now).drain()
    rows = await notes.rows(pool)
    assert len(rows[0]["outputs"]) == 3 and rows[0]["outputs"][0]["error"]
    assert await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone() == (3,)


async def test_over_budget_and_auth_disable_only_notes(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
) -> None:
    channel, _, _ = await seed(pool, database_url)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(401))
    ) as http:
        w = worker(pool, channel, http)
        await w.drain()
        assert w.disabled and not await notes.rows(pool)
        await w.drain()
        capped = notes.NotesWorker(
            pool,
            [channel],
            TRANSCRIBER,
            GroqLLM(http, Secret("k")),
            Budget(pool, Caps(month_usd=Decimal(0))),
        )
        await capped.drain()
    assert [r["status"] for r in await notes.rows(pool)] == ["over_budget", "over_budget"]
    assert await (await conn.execute("SELECT count(*) FROM gate_window")).fetchone() == (6,)


async def test_gate_barrier_history_and_no_gate(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    channel, session, now = await seed(pool, database_url, "league", gate=False)
    assert not await notes.pending(
        pool, [channel], TRANSCRIBER, notes.NotesPolicy(), now, gated=True
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        await worker(pool, channel, http, gated=False).drain()
    rows = await notes.rows(pool, session_id=session)
    assert len(rows) == 4 and all(r["mode"] is None and r["schema_name"] == "general" for r in rows)
    old_channel, old_session, now = await seed(pool, database_url, age=timedelta(hours=13))
    assert not await notes.pending(
        pool, [old_channel], TRANSCRIBER, notes.NotesPolicy(), now, gated=True
    )
    recent, _, now = await seed(pool, database_url, age=timedelta(hours=11))
    assert await notes.pending(pool, [recent], TRANSCRIBER, notes.NotesPolicy(), now, gated=True)
    await conn.execute("UPDATE stream_session SET ended_at=NULL WHERE id=%s", [old_session])
    assert await notes.pending(
        pool, [old_channel], TRANSCRIBER, notes.NotesPolicy(), now, gated=True
    )


async def test_bug_keeps_worker_alive(pool: Pool, database_url: str) -> None:
    channel, _, _ = await seed(pool, database_url)
    calls = 0

    class Bug:
        name = "bug"

        async def complete_json[T](
            self, request: LLMRequest, parse: Callable[[Mapping[str, Any]], T]
        ) -> LLMResult[T]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("bug")
            raw = json.loads(request.user)
            return LLMResult(parse(good(raw["turns"][0]["at_ms"])), "bug", ())

    await notes.NotesWorker(pool, [channel], TRANSCRIBER, Bug(), Budget(pool, Caps())).drain()
    assert [r["status"] for r in await notes.rows(pool)] == ["failed", "ok"]


def test_cli_timeline_and_export(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def setup() -> None:
        async with open_pool(database_url) as pool:
            channel, _, _ = await seed(pool, database_url)
            async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
                await worker(pool, channel, http).drain()

    asyncio.run(setup())
    monkeypatch.setenv("KIROKU_DATABASE_URL", database_url)
    runner = CliRunner()
    listed = runner.invoke(app, ["notes", "list"])
    assert (
        listed.exit_code == 0 and "00:00-03:00" in listed.stdout and "[ok] match" in listed.stdout
    )
    exported = runner.invoke(app, ["notes", "export", "--since", "2026-01-01"])
    assert exported.exit_code == 0
    rows = [json.loads(line) for line in exported.stdout.splitlines()]
    assert len(rows) == 2 and rows[0]["schema"] == {"name": "general", "version": 1}
    assert rows[0]["input"]["turns"] and rows[0]["output"][0]["error"] is None
    assert runner.invoke(app, ["notes", "export", "--since", "invalid"]).exit_code == 2


async def test_mode_tie_uses_latest_and_pack_at_span_start(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
) -> None:
    from kiroku import segments
    from kiroku.source import StreamMeta

    channel, session, now = await seed(pool, database_url, "league")
    await conn.execute(
        "UPDATE gate_window SET mode=CASE WHEN start_ms < 90000 THEN 'playing' ELSE 'chatting' END "
        "WHERE session_id=%s AND start_ms < 180000",
        [session],
    )
    await conn.execute(
        "UPDATE gate_window SET game_key='latest' WHERE session_id=%s AND start_ms=135000",
        [session],
    )
    await segments.record(
        pool, session, 90_000, StreamMeta("changed", "New title", "Just Chatting"), None
    )
    span = (
        await notes.pending(pool, [channel], TRANSCRIBER, notes.NotesPolicy(), now, gated=True)
    )[0]
    async with httpx.AsyncClient() as http:
        pack = LeaguePack(http, Path("/tmp/unused"), PUBLIC_WEBSITE_KEY)
        prepared = await notes.prepare(pool, span, TRANSCRIBER, {"league": pack}, gated=True)
    assert prepared.input["mode"] == "chatting" and prepared.input["pack"] == "league"
    assert prepared.schema.name == "league.chatting" and prepared.game_key == "latest"
    assert prepared.input["live"] and "RED" in str(prepared.input["stream"])


async def test_auth_stops_all_sessions_after_one_call(
    pool: Pool,
    database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, _, _ = await seed(pool, database_url, "league")
    b, _, _ = await seed(pool, database_url, "general")
    calls = 0

    def reject(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        w = notes.NotesWorker(
            pool, [a, b], TRANSCRIBER, GroqLLM(http, Secret("k")), Budget(pool, Caps())
        )
        await w.drain()
    assert calls == 1 and w.disabled
    assert sum(r.levelno == logging.ERROR for r in caplog.records) == 1


async def test_cancelled_retry_preserves_paid_attempt(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
) -> None:
    channel, _, _ = await seed(pool, database_url)
    sleeping = asyncio.Event()
    calls = 0

    async def pause(seconds: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=completion({})) if calls == 1 else httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        w = notes.NotesWorker(
            pool,
            [channel],
            TRANSCRIBER,
            GroqLLM(http, Secret("k"), sleep=pause),
            Budget(pool, Caps()),
        )
        task = asyncio.create_task(w.drain())
        await asyncio.wait_for(sleeping.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not await notes.rows(pool)
    assert await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone() == (1,)
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        await worker(pool, channel, http).drain()
    row = (await notes.rows(pool))[0]
    assert len(row["outputs"]) == 3 and row["outputs"][0]["error"]
    assert await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone() == (3,)


async def test_cancellation_during_commit_preserves_result(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _, _ = await seed(pool, database_url)
    entered, release = asyncio.Event(), asyncio.Event()
    original = notes.commit

    async def slow(*args: Any, **kwargs: Any) -> bool:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(notes, "commit", slow)
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        task = asyncio.create_task(worker(pool, channel, http).drain())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [r["status"] for r in await notes.rows(pool)] == ["ok"]
    assert await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone() == (1,)


async def test_budget_refuses_paid_retry_and_keeps_first_output(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
) -> None:
    from kiroku.llm.groq import MODEL, PRICE, estimate_tokens, request_body

    channel, session, now = await seed(pool, database_url, closed=False)
    span = (
        await notes.pending(pool, [channel], TRANSCRIBER, notes.NotesPolicy(), now, gated=True)
    )[0]
    prepared = await notes.prepare(pool, span, TRANSCRIBER, {}, gated=True)
    budget = Budget(pool, Caps())
    request = LLMRequest(
        notes.SYSTEM,
        notes.canonical(prepared.input),
        Purpose.NOTES,
        budget.guard(session),
        2400,
        30.0,
        300,
    )
    estimate = PRICE.usd(estimate_tokens(request_body(MODEL, request), 2400))
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=completion({}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await notes.NotesWorker(
            pool,
            [channel],
            TRANSCRIBER,
            GroqLLM(http, Secret("k")),
            Budget(pool, Caps(session_usd=estimate)),
        ).drain()
    rows = await notes.rows(pool)
    assert calls == 1 and [r["status"] for r in rows] == ["over_budget"]
    assert len(rows[0]["outputs"]) == 1 and rows[0]["outputs"][0]["error"]
    assert await (await conn.execute("SELECT count(*) FROM cost_ledger")).fetchone() == (1,)


async def test_retry_dataset_keeps_original_input_after_gate_setting_changes(
    pool: Pool,
    database_url: str,
) -> None:
    channel, _, now = await seed(pool, database_url, "league")
    clock = Clock()
    calls = 0

    def respond(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(200, json=completion({}))
            if calls == 1
            else httpx.Response(429, headers={"retry-after": "60"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        pack = LeaguePack(http, Path("/tmp/unused"), PUBLIC_WEBSITE_KEY)
        await notes.NotesWorker(
            pool,
            [channel],
            TRANSCRIBER,
            GroqLLM(http, Secret("k"), clock=clock.now, sleep=clock.sleep),
            Budget(pool, Caps()),
            [pack],
            now=lambda: now,
        ).drain()
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        await worker(pool, channel, http, gated=False).drain()
    row = (await notes.rows(pool))[0]
    assert row["schema_name"] == "general"
    assert row["outputs"][0]["input"]["schema"]["name"] == "league.watching_pro"
    assert row["outputs"][-1]["input"]["schema"]["name"] == "general"


async def test_db_outage_cannot_make_cancelled_commit_wait_forever(
    pool: Pool,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    channel, _, _ = await seed(pool, database_url)
    entered = asyncio.Event()

    async def down(*args: Any, **kwargs: Any) -> bool:
        entered.set()
        raise psycopg.OperationalError("database unavailable")

    monkeypatch.setattr(notes, "commit", down)
    monkeypatch.setattr(notes, "WRITE_GRACE_S", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        task = asyncio.create_task(worker(pool, channel, http).drain())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
    assert not await notes.rows(pool)


async def test_note_and_costs_commit_once_under_racing_receipts(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
) -> None:
    from kiroku.ledger import CostEntry, Tokens

    channel, session, now = await seed(pool, database_url)
    span = (
        await notes.pending(pool, [channel], TRANSCRIBER, notes.NotesPolicy(), now, gated=True)
    )[0]
    prepared = await notes.prepare(pool, span, TRANSCRIBER, {}, gated=True)
    cost = CostEntry("groq", "model", Purpose.NOTES, Tokens(10, 20), Decimal(".0001"), 1)
    outcome = notes.Outcome("ok", good(), model="model", costs=(cost,))
    receipts = await asyncio.gather(
        *(notes.commit(pool, prepared, TRANSCRIBER, outcome) for _ in range(2))
    )
    assert sorted(receipts) == [False, True]
    assert not await notes.commit(pool, prepared, TRANSCRIBER, outcome)
    assert await (await conn.execute("SELECT count(*) FROM note")).fetchone() == (1,)
    assert await (
        await conn.execute(
            "SELECT count(*),sum(usd) FROM cost_ledger WHERE session_id=%s", [session]
        )
    ).fetchone() == (1, Decimal(".0001"))


async def test_cancelled_provider_with_unavailable_journal_still_stops(
    pool: Pool,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _, _ = await seed(pool, database_url)
    entered = asyncio.Event()

    async def blocked(req: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled request should not return")

    async def down(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(notes, "journal", down)
    monkeypatch.setattr(notes, "WRITE_GRACE_S", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as http:
        task = asyncio.create_task(worker(pool, channel, http).drain())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
    assert not await notes.rows(pool)
