"""`kiroku rule`: managing Rules, and testing their wording against stored Windows (#4)."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, LiteralString, cast

import httpx
import pytest
from typer.testing import CliRunner

from kiroku import sessions
from kiroku.cli import app
from kiroku.db import connect, open_pool
from kiroku.gate.windows import Bounds, GateState
from kiroku.gating import NotAsked, Status, commit
from kiroku.sessions import claim_channels
from kiroku.source import StreamMeta

NAME = "groq:whisper-large-v3-turbo"
SPLIT = [
    json.loads(line)
    for line in Path("spikes/results/2026-10-11-gate-split.jsonl").read_text().splitlines()
][1:]
RECORDED = [
    json.loads(line)
    for line in Path("spikes/results/2026-10-12-rule-wording.jsonl").read_text().splitlines()
]
OLD = "Does the speaker give an opinion on a team's draft or hero picks?"
NEW = (
    "Does the speaker talk about picking or banning heroes in this game's draft, such as why "
    "a hero was picked, banned, or should have been picked?"
)


@pytest.fixture
def kiroku(database_url: str) -> Callable[..., Any]:
    runner = CliRunner()
    env = {"KIROKU_DATABASE_URL": database_url, "KIROKU_SECRETS_FILE": "/x", "JEV_API_KEY": "k"}

    def run(*args: str, extra: dict[str, str] | None = None) -> Any:
        return runner.invoke(app, ["rule", *args], env=env | (extra or {}))

    return run


def query(database_url: str, sql: str) -> list[tuple[Any, ...]]:
    async def go() -> list[tuple[Any, ...]]:
        async with connect(database_url) as conn:
            cur = await conn.execute(cast(LiteralString, sql))
            return list(await cur.fetchall())

    return asyncio.run(go())


def test_add_edit_disable_list(kiroku: Callable[..., Any], database_url: str) -> None:
    added = kiroku("add", "predicts_winner", "Does he predict the winner?", "--threshold", "0.6")
    assert added.exit_code == 0, added.output
    assert "predicts_winner (enabled, threshold 0.60, any pack, cooldown 600 s)" in added.output

    again = kiroku("add", "predicts_winner", "Again?")
    assert again.exit_code == 2 and "already exists" in again.stderr
    bad = kiroku("add", "Bad-Name", "x?")
    assert bad.exit_code == 2 and "lowercase letters" in bad.stderr
    ticks = kiroku("add", "sneaky", "Said in `context`?")
    assert ticks.exit_code == 2 and "backticks" in ticks.stderr

    edited = kiroku("edit", "predicts_winner", "--threshold", "0.65", "--pack", "dota")
    assert "threshold 0.65, dota" in edited.output
    assert "any pack" in kiroku("edit", "predicts_winner", "--any-pack").output
    assert "(disabled" in kiroku("disable", "predicts_winner").output
    listed = kiroku("list")
    assert "predicts_winner (disabled" in listed.output
    assert "Does he predict the winner?" in listed.output
    assert "(enabled" in kiroku("enable", "predicts_winner").output
    missing = kiroku("disable", "nope")
    assert missing.exit_code == 2 and "no rule named nope" in missing.stderr
    assert query(database_url, "SELECT count(*) FROM rule") == [(1,)]


@pytest.fixture
def spike_windows(database_url: str) -> int:
    """The spike's eight Windows, stored with the exact state the Gate saw."""

    async def seed() -> int:
        async with (
            open_pool(database_url) as pool,
            claim_channels(database_url, "twitch", ["gorgc"]) as (channel,),
        ):
            meta = StreamMeta("318767459047", "co-stream", "Dota 2")
            session = await sessions.open_session(pool, channel, meta, _now())
            for row in sorted(
                (r for r in SPLIT if r["arm"] == "split" and r["repeat"] == 0),
                key=lambda r: r["chunk"],
            ):
                state = row["request"]["state"]
                start = row["chunk"] * 30_000
                outcome = NotAsked(Status.FAILED, "seeded from the spike")
                gate_state = GateState(state["context"], state["new"])
                await commit(
                    pool, session, NAME, Bounds(start, start + 30_000), gate_state, [], outcome
                )
            return session

    return asyncio.run(seed())


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def replay_jev(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    cassette = {json.dumps(c["request"], sort_keys=True): c["response"] for c in RECORDED}
    sent: list[httpx.Request] = []

    def jev(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        body = cassette.get(json.dumps(json.loads(request.content), sort_keys=True))
        if body is None:
            pytest.fail("Rule test request changed: rerun spikes/rule_wording.py")
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(jev), **kwargs)

    monkeypatch.setattr("kiroku.cli.httpx.AsyncClient", client)
    return sent


def test_rule_test_separates_a_vague_rule_from_a_reworded_one(
    kiroku: Callable[..., Any],
    database_url: str,
    spike_windows: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = replay_jev(monkeypatch)
    assert kiroku("add", "draft_opinion", OLD).exit_code == 0

    vague = kiroku("test", "draft_opinion", "--session", str(spike_windows))
    assert vague.exit_code == 0, vague.output
    # Spike finding 3: any talk about heroes passes, including in-game play.
    assert "draft_opinion: 5 of 8 Windows pass at 0.70" in vague.output

    reworded = kiroku(
        "test", "draft_opinion", "--session", str(spike_windows), "--instructions", NEW
    )
    assert reworded.exit_code == 0, reworded.output
    # Only the three Windows that actually discuss picks and bans pass.
    assert "draft_opinion: 3 of 8 Windows pass at 0.7" in reworded.output
    assert "Lowest:" in reworded.output and "Highest:" in reworded.output

    assert len(sent) == 16
    # Every dry run was billed to the month, to no session, and stored no decisions.
    assert query(
        database_url,
        "SELECT count(*), count(session_id) FROM cost_ledger WHERE purpose = 'rule_test'",
    ) == [(16, 0)]
    assert query(database_url, "SELECT count(*) FROM gate_decision") == [(0,)]


def test_rule_test_stops_at_the_month_cap(
    kiroku: Callable[..., Any], spike_windows: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = replay_jev(monkeypatch)
    assert kiroku("add", "draft_opinion", OLD).exit_code == 0
    capped = kiroku(
        "test",
        "draft_opinion",
        "--session",
        str(spike_windows),
        extra={"KIROKU_MONTH_CAP_USD": "0"},
    )
    assert capped.exit_code == 0, capped.output
    assert "0 of 0 Windows pass" in capped.output and "(8 not scored)" in capped.output
    assert "month Budget cap $0 reached" in capped.output
    assert sent == []


def test_rule_test_without_windows_says_so(kiroku: Callable[..., Any]) -> None:
    assert kiroku("add", "draft_opinion", OLD).exit_code == 0
    empty = kiroku("test", "draft_opinion")
    assert empty.exit_code == 2 and "no stored Windows" in empty.stderr


def test_bad_input_is_a_one_line_error(kiroku: Callable[..., Any]) -> None:
    for args, message in [
        (("add", "x", "Q?", "--threshold", "abc"), "is not a number"),
        (("add", "x", "Q?", "--threshold", "nan"), "must be above 0"),
        (("add", "x", "Q?", "--threshold", "0.705"), "at most two decimals"),
        (("add", "x", "Q?", "--cooldown-s", "-5"), "can't be negative"),
        (("add", "x", "Q?", "--pack", "lol"), "use one of league, dota"),
        (("test", "x", "--last", "0"), "--last"),
    ]:
        result = kiroku(*args)
        assert result.exit_code == 2, (args, result.output)
        assert message in result.stderr, (args, result.stderr)
        assert "Traceback" not in result.output
    scoped = kiroku("add", "x", "Q?", "--pack", "league")
    assert scoped.exit_code == 0 and "aren't wired into the live Gate yet" in scoped.stderr


def test_a_rewording_is_tested_at_the_saved_threshold(
    kiroku: Callable[..., Any], spike_windows: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay_jev(monkeypatch)
    assert kiroku("add", "draft_opinion", OLD, "--threshold", "0.9").exit_code == 0
    reworded = kiroku(
        "test", "draft_opinion", "--session", str(spike_windows), "--instructions", NEW
    )
    assert "draft_opinion: 2 of 8 Windows pass at 0.90" in reworded.output
