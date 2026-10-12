"""Issue #3 acceptance 1, offline: the spike fixture through the production Gate path.

The Jev answers come from spikes/results/2026-10-11-gate-split.jsonl, recorded live by
spikes/gate_split.py (the `split` arm, first repeat). Requests are looked up by their exact
body, so changing the wording, the state layout or the model fails here until the live
evaluation is re-run: CI holds the code to what was measured.
"""

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection

from kiroku import rules, sessions, transcript
from kiroku.budget import Unlimited
from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.gate.jev import JevGate
from kiroku.gate.windows import WindowPolicy
from kiroku.gating import GateLoop
from kiroku.sessions import claim_channels
from kiroku.source import StreamMeta
from kiroku.transcriber import Result, Utterance

NAME = "groq:whisper-large-v3-turbo"
CHUNKS = [
    json.loads(line)["text"]
    for line in Path("spikes/results/2026-10-11-gorgc-blast-slam.jsonl").read_text().splitlines()
]
RECORDED = [
    json.loads(line)
    for line in Path("spikes/results/2026-10-11-gate-split.jsonl").read_text().splitlines()
]
LEAGUE_RECORDED = [
    json.loads(line)
    for line in Path("spikes/results/2026-10-12-league-gate.jsonl").read_text().splitlines()
]


def _canonical(body: Any) -> str:
    return json.dumps(body, sort_keys=True)


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


async def test_the_split_passes_real_predictions_but_not_the_one_in_context(
    pool: Pool, database_url: str, conn: AsyncConnection
) -> None:
    header, calls = RECORDED[0]["header"], RECORDED[1:]
    cassette = {
        _canonical(c["request"]): c["response"]
        for c in calls
        if c["arm"] == "split" and c["repeat"] == 0
    }
    for r in header["rules"]:  # ids 1-3 in a fresh database, as in the recording
        await rules.add(pool, r["name"], r["instructions"], threshold=Decimal(r["threshold"]))

    async with claim_channels(database_url, "twitch", ["gorgc"]) as (channel,):
        meta = StreamMeta("318767459047", header["stream"]["title"], "Dota 2")
        session = await sessions.open_session(pool, channel, meta, datetime.now(UTC))
    leg = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg, datetime.now(UTC))
    chunk_ms = header["chunk_ms"]
    for i, text in enumerate(CHUNKS):  # one span and one Utterance per chunk
        start, end = i * chunk_ms, (i + 1) * chunk_ms
        result = Result(start, end, (Utterance(start, end, text),), None)
        await transcript.commit(pool, session, NAME, result)

    def jev(request: httpx.Request) -> httpx.Response:
        body = cassette.get(_canonical(json.loads(request.content)))
        if body is None:
            pytest.fail(
                "Gate request changed since the live recording; re-run spikes/gate_split.py"
            )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(jev)) as http:
        loop = GateLoop(
            pool,
            session,
            NAME,
            JevGate(http, Secret("k")),
            Unlimited(),
            lambda: dict(header["stream"]),
            policy=WindowPolicy(length_ms=chunk_ms),
        )
        loop.finish(session_ended=True)
        await loop.run()

    cur = await conn.execute(
        """
        SELECT w.start_ms / %s, d.passed FROM gate_decision d
        JOIN gate_window w ON w.id = d.window_id JOIN rule r ON r.id = d.rule_id
        WHERE r.name = 'predicts_winner' ORDER BY w.start_ms
        """,
        [chunk_ms],
    )
    passed = dict(await cur.fetchall())
    assert (passed[0], passed[1], passed[7]) == (True, False, True)
    cur = await conn.execute(
        "SELECT count(*), count(DISTINCT (window_id, rule_id)) FROM gate_decision"
    )
    assert await cur.fetchone() == (8 * 3, 8 * 3)
    cur = await conn.execute("SELECT DISTINCT mode, model FROM gate_window")
    assert await cur.fetchall() == [("watching_pro", "jev:jev-1.13.0")]
    cur = await conn.execute("SELECT count(*) FROM cost_ledger WHERE purpose = 'gate'")
    assert await cur.fetchone() == (8,)


@pytest.mark.parametrize("repeat", [0, 1, 2])
async def test_league_live_context_replays_real_jev_answers(
    pool: Pool,
    database_url: str,
    conn: AsyncConnection,
    repeat: int,
) -> None:
    from kiroku import segments
    from kiroku.pack import LiveContext

    recording = LEAGUE_RECORDED
    header = recording[0]["header"]
    calls = [r for r in recording[1:] if r["arm"] == "live" and r["repeat"] == repeat]
    cassette = {_canonical(r["request"]): r["response"] for r in calls}
    for r in header["rules"]:
        await rules.add(
            pool, r["name"], r["instructions"], threshold=Decimal(r["threshold"]), pack="league"
        )
    async with claim_channels(database_url, "twitch", ["caedrel"]) as (channel,):
        session = await sessions.open_session(
            pool,
            channel,
            StreamMeta("recorded", header["stream"]["title"], "League of Legends"),
            datetime.now(UTC),
        )
    leg = await sessions.next_leg(pool, channel, session)
    await segments.record(
        pool,
        session,
        0,
        StreamMeta("recorded", header["stream"]["title"], "League of Legends"),
        "league",
    )
    await sessions.record_leg(pool, leg, datetime.now(UTC))
    for i, text in enumerate(header["texts"]):
        await transcript.commit(
            pool,
            session,
            NAME,
            Result(
                i * 45000, (i + 1) * 45000, (Utterance(i * 45000, (i + 1) * 45000, text),), None
            ),
        )

    def jev(request: httpx.Request) -> httpx.Response:
        body = cassette.get(_canonical(json.loads(request.content)))
        if body is None:
            pytest.fail("League Gate request changed; re-run spikes/league_gate.py")
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(jev)) as http:
        loop = GateLoop(
            pool,
            session,
            NAME,
            JevGate(http, Secret("k")),
            Unlimited(),
            lambda: header["stream"],
            use_segments=True,
            live=lambda bounds: LiveContext(header["live"], header["game_key"]),
        )
        loop.finish(session_ended=True)
        await loop.run()
    cur = await conn.execute("""
        SELECT w.start_ms, r.name, d.passed FROM gate_decision d
        JOIN gate_window w ON w.id=d.window_id JOIN rule r ON r.id=d.rule_id
        ORDER BY w.start_ms,r.name
    """)
    assert await cur.fetchall() == [
        (0, "draft", False),
        (0, "yunara_broken", False),
        (45000, "draft", False),
        (45000, "yunara_broken", True),
    ]
    cur = await conn.execute("SELECT mode FROM gate_window ORDER BY start_ms")
    assert [row[0] for row in await cur.fetchall()] == [r["mode"] for r in calls]
