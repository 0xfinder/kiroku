"""The real HTTP boundary and PostgreSQL, including commit-driven streaming."""

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from psycopg import AsyncConnection
from typer.testing import CliRunner

from kiroku import dry_run, labels, rules, sessions, transcript
from kiroku.budget import Caps
from kiroku.cli import app as cli
from kiroku.config import Secret, Secrets, Settings
from kiroku.db import Pool, open_pool
from kiroku.notify import Delivery, Tap
from kiroku.transcriber import Result, Utterance
from kiroku.web.app import create_app
from kiroku.web.auth import COOKIE
from tests.fakes.oidc import config
from tests.fakes.web import T0, TRANSCRIBER, seed


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as pool:
        yield pool


@pytest.fixture
async def notebook(pool: Pool, database_url: str) -> tuple[int, int]:
    return await seed(pool, database_url)


def settings(database_url: str) -> Settings:
    return Settings(
        database_url,
        Path("/tmp/unused-web"),
        Secrets({}, Path("/tmp/absent-web-key")),
        Caps(),
        oidc=config(),
    )


def owner_cookie(application: Any) -> dict[str, str]:
    return {COOKIE: application.state.auth.signer.dumps(config().owner_email)}


@pytest.fixture
async def client(database_url: str) -> AsyncGenerator[httpx.AsyncClient]:
    application = create_app(settings(database_url))
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="https://testserver",
            cookies=owner_cookie(application),
        ) as client,
    ):
        yield client


async def completed_test(client: httpx.AsyncClient, response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    async with asyncio.timeout(5):
        while True:
            status = await client.get(f"/api/rule-tests/{job_id}")
            assert status.status_code == 200
            job = status.json()
            if job["status"] != "running":
                assert job["status"] == "completed", job
                assert job["completed"] == job["total"]
                return job["report"]
            await asyncio.sleep(0.01)


async def test_session_views(
    client: httpx.AsyncClient, notebook: tuple[int, int], conn: AsyncConnection, pool: Pool
) -> None:
    session, decision = notebook
    response = await client.get("/api/sessions?live=1&limit=1")
    assert response.status_code == 200
    data = response.json()[0]
    assert data["id"] == session and data["channel"] == "caedrel"
    assert data["category"] == "League of Legends" and data["mode"] == "watching_pro"
    assert (data["alert_count"], data["note_count"]) == (1, 2)
    assert (await client.get("/api/sessions?live=0")).json() == []
    assert (await client.get(f"/api/sessions/{session}")).json() == data
    rows = (await client.get(f"/api/sessions/{session}/transcript?after_ms=0&limit=2")).json()
    assert [r["audio_start_ms"] for r in rows] == [5000, 10000]
    notes = (await client.get(f"/api/sessions/{session}/notes")).json()
    assert [n["status"] for n in notes] == ["ok", "pending"]
    assert "input" not in notes[0] and "outputs" not in notes[0]
    assert notes[0]["body"]["summary"].startswith("Blue")
    passes = (await client.get(f"/api/sessions/{session}/passes")).json()
    assert [p["start_ms"] for p in passes] == [45000, 135000]
    row = await (
        await conn.execute("SELECT id FROM alert WHERE decision_id=%s", [decision])
    ).fetchone()
    assert row is not None
    assert await labels.record(pool, Tap(row[0], False, None, Delivery("fixture:1"), T0))
    alerts = (await client.get(f"/api/sessions/{session}/alerts")).json()
    assert alerts[0]["outcome"] == "sent" and alerts[0]["label"] is False
    assert "stronger draft" in alerts[0]["text"]
    channel = await sessions.find_channel(pool, "twitch", "caedrel")
    assert channel is not None
    await sessions.close_open_sessions(pool, channel, T0)
    assert (await client.get("/api/sessions?live=1")).json() == []
    assert len((await client.get("/api/sessions?live=0")).json()) == 1


@pytest.mark.parametrize(
    "suffix", ["", "/transcript", "/notes", "/alerts", "/passes", "/transcript/stream"]
)
async def test_missing_session(client: httpx.AsyncClient, suffix: str) -> None:
    assert (await client.get("/api/sessions/999" + suffix)).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions?limit=0",
        "/api/sessions?limit=101",
        "/api/sessions?live=maybe",
        "/api/review?limit=101",
    ],
)
async def test_query_errors(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 422


async def test_transcript_grouped_offsets(
    client: httpx.AsyncClient, notebook: tuple[int, int], pool: Pool
) -> None:
    session, _ = notebook
    await transcript.commit(
        pool,
        session,
        "fixture:comparison",
        Result(0, 5000, (Utterance(0, 4000, "comparison"),), None),
    )
    rows = (await client.get(f"/api/sessions/{session}/transcript?limit=1")).json()
    assert len(rows) == 2 and all(r["audio_start_ms"] == 0 for r in rows)
    assert (await client.get(f"/api/sessions/{session}/transcript?after_ms=-2")).status_code == 422
    assert (await client.get(f"/api/sessions/{session}/transcript?limit=1001")).status_code == 422


async def test_review_band_verdict_undo_and_telegram_independence(
    client: httpx.AsyncClient, notebook: tuple[int, int], conn: AsyncConnection, pool: Pool
) -> None:
    _, decision = notebook
    row = await (
        await conn.execute("SELECT id FROM alert WHERE decision_id=%s", [decision])
    ).fetchone()
    assert row is not None
    assert await labels.record(pool, Tap(row[0], False, None, Delivery("fixture:1"), T0))
    queue = (await client.get("/api/review")).json()
    assert [Decimal(r["probability"]) for r in queue] == [Decimal(".7"), Decimal(".3")]
    assert queue[0]["id"] == decision and "new" in queue[0] and queue[0]["earlier"]
    assert (await client.post(f"/api/review/{decision}", json={"applies": True})).status_code == 204
    assert len((await client.get("/api/review")).json()) == 1
    assert (await client.get(f"/api/sessions/{notebook[0]}/alerts")).json()[0]["label"] is False
    assert (
        await client.post(f"/api/review/{decision}", json={"applies": False})
    ).status_code == 204
    assert await (
        await conn.execute(
            "SELECT source,applies FROM label WHERE decision_id=%s ORDER BY source", [decision]
        )
    ).fetchall() == [("review", False), ("telegram", False)]
    assert (await client.delete(f"/api/review/{decision}")).status_code == 204
    assert len((await client.get("/api/review")).json()) == 2
    assert await (
        await conn.execute("SELECT source FROM label WHERE decision_id=%s", [decision])
    ).fetchall() == [("telegram",)]
    assert (await client.post("/api/review/999", json={"applies": True})).status_code == 404
    assert (
        await client.post(f"/api/review/{decision}", json={"applies": "false"})
    ).status_code == 422
    assert (
        await client.post(
            f"/api/review/{decision}",
            json={"applies": True},
            headers={"Origin": "https://other.example"},
        )
    ).status_code == 403


async def test_rule_crud(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/rules")).json() == []
    body = {
        "name": "winner",
        "instructions": "Does he predict a winner?",
        "pack": "league",
        "enabled": False,
    }
    response = await client.post("/api/rules", json=body)
    assert response.status_code == 201
    assert response.json()["enabled"] is False
    assert (await client.post("/api/rules", json=body)).status_code == 400
    changed = await client.patch(
        "/api/rules/winner",
        json={
            "instructions": "Does he give odds?",
            "threshold": ".65",
            "cooldown_s": 30,
            "pack": None,
            "enabled": True,
        },
    )
    assert changed.status_code == 200 and changed.json()["pack"] is None
    assert Decimal(changed.json()["threshold"]) == Decimal(".65")
    for action, enabled in (("disable", False), ("enable", True)):
        assert (await client.post(f"/api/rules/winner/{action}")).json()["enabled"] is enabled
    assert len((await client.get("/api/rules")).json()) == 1
    for path in ("/api/rules/missing/enable", "/api/rules/missing/disable"):
        assert (await client.post(path)).status_code == 400
    assert (await client.patch("/api/rules/missing", json={})).status_code == 400
    for patch in (
        {"threshold": 0},
        {"threshold": 0.655},
        {"cooldown_s": -1},
        {"instructions": "`new`"},
        {"pack": "invalid"},
        {"instructions": ""},
    ):
        assert (await client.patch("/api/rules/winner", json=patch)).status_code == 400
    assert (
        await client.post("/api/rules", json={"name": "BAD", "instructions": "Question"})
    ).status_code == 400
    assert (await client.patch("/api/rules/winner", json={"unexpected": 1})).status_code == 422


@pytest.mark.parametrize("initial_enabled", [False, True])
async def test_rule_edit_preserves_concurrent_enablement(
    client: httpx.AsyncClient, pool: Pool, monkeypatch: pytest.MonkeyPatch, initial_enabled: bool
) -> None:
    await rules.add(
        pool,
        "winner",
        "Does he predict a winner?",
        threshold=Decimal(".7"),
        enabled=initial_enabled,
    )
    original = rules.get

    async def toggle_after_read(pool: Pool, name: str, *, owner: str = "me") -> rules.Rule:
        old = await original(pool, name, owner=owner)
        await rules.set_enabled(pool, name, not initial_enabled, owner=owner)
        return old

    monkeypatch.setattr(rules, "get", toggle_after_read)
    response = await client.patch("/api/rules/winner", json={"instructions": "Does he give odds?"})
    assert response.status_code == 200
    assert response.json()["instructions"] == "Does he give odds?"
    assert response.json()["enabled"] is not initial_enabled
    assert (await original(pool, "winner")).enabled is not initial_enabled


async def test_rule_test_needs_key_lazily_and_bounded_selectors(
    client: httpx.AsyncClient, notebook: tuple[int, int]
) -> None:
    session, _ = notebook
    for body in ({"last": 101}, {"last": 0}, {"session": session, "last": 1}, {"session": 0}):
        assert (await client.post("/api/rules/predicts_winner/test", json=body)).status_code == 422
    assert (await client.post("/api/rules/missing/test", json={})).status_code == 400
    response = await client.post("/api/rules/predicts_winner/test", json={"last": 1})
    assert response.status_code == 400 and "JEV_API_KEY" in response.json()["detail"]
    assert (
        await client.post("/api/rules/predicts_winner/test", json={"session": 999})
    ).status_code == 400
    assert (
        await client.post("/api/rules/predicts_winner/test", json={"threshold": 0})
    ).status_code == 400


async def test_rule_test_recorded_jev_and_month_costs(
    database_url: str, pool: Pool, conn: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiroku import gating, rules
    from kiroku.gate.windows import Bounds, GateState
    from kiroku.gating import NotAsked, Status
    from kiroku.source import StreamMeta
    from tests.test_rule_cli import NEW, OLD, SPLIT, replay_jev

    async with sessions.claim_channels(database_url, "twitch", ["gorgc"]) as channels:
        session = await sessions.open_session(
            pool, channels[0], StreamMeta("fixture", "co-stream", "Dota 2"), T0
        )
    await rules.add(pool, "draft_opinion", OLD, threshold=Decimal(".7"))
    for row in sorted(
        (r for r in SPLIT if r["arm"] == "split" and r["repeat"] == 0), key=lambda r: r["chunk"]
    ):
        state = row["request"]["state"]
        start = row["chunk"] * 30_000
        await gating.commit(
            pool,
            session,
            TRANSCRIBER,
            Bounds(start, start + 30000),
            GateState(state["context"], state["new"]),
            [],
            NotAsked(Status.FAILED, "fixture"),
        )
    cfg = replace(
        settings(database_url),
        secrets=Secrets({"JEV_API_KEY": Secret("fixture").reveal()}, Path("/tmp/absent")),
    )
    application = create_app(cfg)
    # Construct HTTP transport before patching the provider's httpx client factory.
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="https://testserver",
            cookies=owner_cookie(application),
        ) as client,
    ):
        sent = replay_jev(monkeypatch)
        response = await client.post(
            "/api/rules/draft_opinion/test", json={"session": session, "instructions": NEW}
        )
        report = await completed_test(client, response)
        assert (report["passed"], report["scored"], sum(report["histogram"])) == (3, 8, 8)
        assert report["pass_rate"] == 3 / 8 and len(report["highest"]) == 3
        assert "3 of 8" in report["report"] and len(sent) == 8
        second = await client.post(
            "/api/rules/draft_opinion/test", json={"last": 2, "instructions": NEW}
        )
        assert (await completed_test(client, second))["scored"] == 2
        assert (await client.get(f"/api/rule-tests/{response.json()['id']}")).status_code == 404
    assert await (
        await conn.execute(
            "SELECT count(*),count(session_id) FROM cost_ledger WHERE purpose='rule_test'"
        )
    ).fetchone() == (10, 0)
    assert await (await conn.execute("SELECT count(*) FROM gate_decision")).fetchone() == (0,)


@pytest.fixture
async def server(database_url: str) -> AsyncGenerator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings(database_url)), log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def next_event(lines: AsyncIterator[str]) -> tuple[str, list[dict[str, Any]]]:
    event_id = ""
    data = ""
    async with asyncio.timeout(10):
        async for line in lines:
            if line.startswith("id: "):
                event_id = line[4:]
            if line.startswith("data: "):
                data = line[6:]
            if not line and event_id and data:
                return event_id, json.loads(data)
    raise AssertionError("stream ended")


async def test_real_sse_commit_reconnect_backfill_and_keepalive(
    server: str, notebook: tuple[int, int], pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiroku.web.stream.POLL_S", 0.05)
    monkeypatch.setattr("kiroku.web.stream.KEEPALIVE_S", 0.05)
    session, _ = notebook
    path = f"/api/sessions/{session}/transcript/stream?after_ms=175000"
    async with httpx.AsyncClient(base_url=server) as client:
        async with client.stream("GET", path) as response:
            assert response.status_code == 200 and response.headers["content-type"].startswith(
                "text/event-stream"
            )
            lines = response.aiter_lines()
            assert await anext(lines) == ": connected"
            await transcript.commit(
                pool,
                session,
                TRANSCRIBER,
                Result(180000, 185000, (Utterance(180000, 184000, "New committed speech"),), None),
            )
            event_id, rows = await next_event(lines)
            assert event_id == "180000" and rows[0]["text"] == "New committed speech"
            async with asyncio.timeout(5):
                async for line in lines:
                    assert line != "event: reconcile", "ordinary append forced a full refetch"
                    if line == ": keepalive":
                        break
        await transcript.commit(
            pool,
            session,
            TRANSCRIBER,
            Result(185000, 190000, (Utterance(185000, 189000, "After reconnect"),), None),
        )
        async with client.stream("GET", path, headers={"Last-Event-ID": event_id}) as response:
            lines = response.aiter_lines()
            event_id, rows = await next_event(lines)
            assert event_id == "185000" and rows[0]["text"] == "After reconnect"
            # Behind the audio cursor; a second transcriber provides historical comparison.
            await transcript.commit(
                pool,
                session,
                "fixture:backfill",
                Result(0, 1000, (Utterance(0, 1000, "Late Backfill"),), None),
            )
            async with asyncio.timeout(5):
                async for line in lines:
                    if line == "event: reconcile":
                        assert json.loads((await anext(lines)).removeprefix("data: ")) == {
                            "after_ms": -1
                        }
                        break
            async with asyncio.timeout(5):
                async for line in lines:
                    assert not line.startswith("id: "), "reconnect duplicated speech"
                    if line == ": keepalive":
                        break
        for header in ("garbage", "-2", "2147483648"):
            assert (await client.get(path, headers={"Last-Event-ID": header})).status_code == 400


async def test_notify_transaction_rolls_back_without_delivery(
    database_url: str, pool: Pool, notebook: tuple[int, int]
) -> None:
    import psycopg

    session, _ = notebook
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as listener:
        await listener.execute("LISTEN kiroku_transcript")
        async with pool.connection() as conn:
            await conn.execute("BEGIN")
            await conn.execute("SELECT pg_notify('kiroku_transcript',%s)", [str(session)])
            assert [n async for n in listener.notifies(timeout=0.05, stop_after=1)] == []
            await conn.execute("ROLLBACK")
            assert [n async for n in listener.notifies(timeout=0.05, stop_after=1)] == []
        await transcript.commit(pool, session, TRANSCRIBER, Result(180000, 185000, (), None))
        notifications = [n async for n in listener.notifies(timeout=1, stop_after=1)]
        assert len(notifications) == 1 and notifications[0].payload == str(session)
        assert not await transcript.commit(
            pool, session, TRANSCRIBER, Result(180000, 185000, (), None)
        )
        assert [n async for n in listener.notifies(timeout=0.05, stop_after=1)] == []


async def test_static_spa_and_unknown_api(client: httpx.AsyncClient) -> None:
    for path in ("/", "/notes", "/session/1", "/rules", "/review"):
        response = await client.get(path)
        assert response.status_code == 200 and "<!doctype html>" in response.text.lower()
    assert (await client.get("/_app/immutable/missing.js")).status_code == 404
    response = await client.get("/api/missing")
    assert response.status_code == 404 and response.json()["detail"] == "API route not found"


def test_non_loopback_bind_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[dict[str, Any]] = []

    def run(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("uvicorn.run", run)
    with caplog.at_level(logging.WARNING):
        assert CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0"]).exit_code == 0
    assert "D35" in caplog.text and "owner login" in caplog.text
    assert calls[0]["workers"] == 1
    assert calls[0]["proxy_headers"] is False
    assert calls[0]["timeout_graceful_shutdown"] == 5
    caplog.clear()
    assert CliRunner().invoke(cli, ["serve"]).exit_code == 0
    assert "D35" not in caplog.text and calls[1]["host"] == "127.0.0.1"


async def test_serve_shutdown_with_open_stream(
    database_url: str, notebook: tuple[int, int]
) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = os.environ | {
        "KIROKU_DATABASE_URL": database_url,
        "KIROKU_SECRETS_FILE": "tests/absent-web-secrets.env",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kiroku",
        "serve",
        "--port",
        str(port),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            async with asyncio.timeout(10):
                while True:
                    try:
                        if (await client.get("/api/sessions")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        if proc.returncode is not None:
                            raise AssertionError("serve exited before startup") from None
                    await asyncio.sleep(0.05)
            async with client.stream(
                "GET", f"/api/sessions/{notebook[0]}/transcript/stream"
            ) as response:
                assert response.status_code == 200
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.communicate(), 12)
                assert proc.returncode in (0, -signal.SIGTERM)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()


async def test_rule_test_month_cap_reports_refusal_without_dispatch(
    database_url: str, notebook: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(
        settings(database_url),
        caps=Caps(month_usd=Decimal(0)),
        secrets=Secrets({"JEV_API_KEY": "fixture"}, Path("/tmp/absent")),
    )
    application = create_app(cfg)
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="https://testserver",
            cookies=owner_cookie(application),
        ) as client,
    ):

        async def forbidden(*args: Any, **kwargs: Any) -> httpx.Response:
            raise AssertionError("monthly budget refusal sent a paid request")

        monkeypatch.setattr(httpx.AsyncClient, "post", forbidden)
        # Use request() so only the provider's post() is prohibited.
        response = await client.request("POST", "/api/rules/predicts_winner/test", json={"last": 1})
        report = await completed_test(client, response)
        assert report["scored"] == 0 and report["failed"] == 1
        assert "month" in report["trials"][0]["error"]


async def test_session_specific_rule_test_is_bounded(pool: Pool, notebook: tuple[int, int]) -> None:
    from kiroku import dry_run, gating
    from kiroku.gate.windows import Bounds, GateState
    from kiroku.gating import NotAsked, Status

    session, _ = notebook
    for index in range(4, 110):
        await gating.commit(
            pool,
            session,
            TRANSCRIBER,
            Bounds(index * 45000, (index + 1) * 45000),
            GateState({}, "Stored speech"),
            [],
            NotAsked(Status.FAILED, "fixture"),
        )
    assert len(await dry_run.stored_windows(pool, session_id=session, last=100)) == 100
    assert len(await dry_run.stored_windows(pool, session_id=session, last=None)) == 110


@pytest.mark.parametrize(
    "origin,forwarded,accepted",
    [
        ("https://testserver", "kiroku.example", True),
        ("https://testserver", "evil.example", True),
        ("http://testserver", "kiroku.example", False),
        ("https://evil.example", "kiroku.example", False),
        ("https://kiroku.example.evil", "kiroku.example", False),
        ("https://kiroku.example", "kiroku.example:443", False),
        ("https://kiroku.example", "", False),
        ("null", "", False),
    ],
)
async def test_proxy_write_origin(
    client: httpx.AsyncClient, origin: str, forwarded: str, accepted: bool
) -> None:
    response = await client.post(
        "/api/rules",
        headers={"Origin": origin, "X-Forwarded-Host": forwarded},
        json={"name": "proxy", "instructions": "Does he predict a winner?"},
    )
    assert response.status_code == (201 if accepted else 403)
    assert "access-control-allow-origin" not in response.headers
    preflight = await client.options(
        "/api/rules",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Headers": "x-forwarded-host",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.parametrize(
    "method",
    ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "CUSTOM"],
)
async def test_unknown_api_methods_are_json(client: httpx.AsyncClient, method: str) -> None:
    response = await client.request(method, "/api/nonexistent")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    if method == "HEAD":
        assert response.content == b""
    else:
        assert response.json() == {"detail": "API route not found"}
    unsupported = await client.request(
        method if method not in {"GET", "HEAD"} else "PUT", "/api/sessions"
    )
    assert unsupported.status_code in {404, 405}
    assert unsupported.headers["content-type"] == "application/json"


async def test_head_on_get_routes(client: httpx.AsyncClient, notebook: tuple[int, int]) -> None:
    session, _ = notebook
    for path in [
        "/api/sessions",
        f"/api/sessions/{session}",
        f"/api/sessions/{session}/transcript",
        "/api/rules",
        "/api/review",
    ]:
        get = await client.get(path)
        head = await client.head(path)
        assert head.status_code == get.status_code == 200
        assert head.headers["content-type"] == get.headers["content-type"]
        assert head.headers["content-length"] == get.headers["content-length"]
        assert head.content == b""
    async with asyncio.timeout(1):
        head = await client.head(f"/api/sessions/{session}/transcript/stream")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-type"].startswith("text/event-stream")


async def test_rule_job_progress_disconnect_errors_and_shutdown(
    database_url: str, notebook: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(
        settings(database_url), secrets=Secrets({"JEV_API_KEY": "fixture"}, Path("/tmp/absent"))
    )
    application = create_app(cfg)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    runs = 0

    async def run(*args: Any, **kwargs: Any) -> list[dry_run.Trial]:
        nonlocal runs
        runs += 1
        windows = args[4]
        first = dry_run.Trial(windows[0], 0.8)
        kwargs["progress"](first)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        if runs == 2:
            raise rules.RuleError("test provider unavailable")
        return [first]

    monkeypatch.setattr(dry_run, "run", run)
    transport = httpx.ASGITransport(app=application)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", cookies=owner_cookie(application)
        ) as client:
            async with asyncio.timeout(1):
                response = await client.post("/api/rules/predicts_winner/test", json={"last": 1})
            assert response.status_code == 202
            job_id = response.json()["id"]
            await started.wait()
            progress = (await client.get(f"/api/rule-tests/{job_id}")).json()
            assert progress == {"id": job_id, "status": "running", "completed": 1, "total": 1}
            assert (
                await client.post("/api/rules/predicts_winner/test", json={})
            ).status_code == 409
        # Losing the requesting client leaves the paid job and its retrievable report intact.
        release.set()
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", cookies=owner_cookie(application)
        ) as client:
            report = await completed_test(client, response)
            assert report["scored"] == 1
            assert (await client.get(f"/api/rule-tests/{job_id}")).json()["report"] == report
            error_job = await client.post("/api/rules/predicts_winner/test", json={"last": 1})
            assert error_job.status_code == 202
            error_id = error_job.json()["id"]
            async with asyncio.timeout(1):
                while True:
                    job = (await client.get(f"/api/rule-tests/{error_id}")).json()
                    if job["status"] != "running":
                        break
                    await asyncio.sleep(0.01)
            assert job["status"] == "failed" and job["error"] == "test provider unavailable"
            assert (await client.get(f"/api/rule-tests/{job_id}")).status_code == 404
            release.clear()
            started.clear()
            assert (
                await client.post("/api/rules/predicts_winner/test", json={"last": 1})
            ).status_code == 202
            await started.wait()
    assert cancelled.is_set(), "server shutdown left a paid job running"


async def test_sse_catches_backfill_between_snapshot_and_subscription(
    server: str, pool: Pool, notebook: tuple[int, int]
) -> None:
    session, _ = notebook
    async with httpx.AsyncClient(base_url=server) as client:
        cursor = (await client.get(f"/api/sessions/{session}/transcript/cursor")).json()["id"]
        await transcript.commit(
            pool,
            session,
            "fixture:gap",
            Result(7000, 8000, (Utterance(7000, 7500, "Between scan and stream"),), None),
        )
        snapshot = (
            await client.get(f"/api/sessions/{session}/transcript?through_id={cursor}&limit=1000")
        ).json()
        assert all(row["text"] != "Between scan and stream" for row in snapshot)
        async with client.stream(
            "GET", f"/api/sessions/{session}/transcript/stream?after_ms=175000&after_id={cursor}"
        ) as response:
            lines = response.aiter_lines()
            async with asyncio.timeout(5):
                async for line in lines:
                    if line == "event: reconcile":
                        assert json.loads((await anext(lines)).removeprefix("data: ")) == {
                            "after_ms": 6999
                        }
                        break
                else:
                    pytest.fail("Backfill between snapshot and subscription was missed")
