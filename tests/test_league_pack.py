"""Recorded lolesports + Data Dragon -> Pack -> live transcript -> Gate -> Postgres."""

import json
import os
from collections.abc import AsyncGenerator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection

from kiroku import rules
from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.gate.jev import JevGate
from kiroku.packs.league import LeaguePack
from kiroku.packs.league.data import DATA_TTL_S, DataError, match_context, names, select_event
from kiroku.transcriber.groq import build_prompt
from tests.test_live_replay import GroqStub, query_rows, spans_cover, watch_until
from tests.test_live_replay import fake_stream as fake_stream

FIXTURES = Path(__file__).parent / "fixtures" / "league"
TITLE = "DEMACIA CUP FLY VS RED - LAST SPOTS IN THE TOP 8"


def read(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class LeagueReplay:
    def __init__(self, *, fail: str | None = None, live: bool = True) -> None:
        self.fail = fail
        self.live = live
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        endpoint = path.rsplit("/", 1)[-1]
        if self.fail and self.fail in path:
            return httpx.Response(503)
        if "/window/" in path:
            body = read("window")
        elif endpoint == "getLive" and self.live:
            # Real completed match, with ONLY lifecycle flags changed to replay live routing.
            body = read("getSchedule")
            events = body["data"]["schedule"]["events"]
            event = next(e for e in events if e.get("match", {}).get("id") == "117133830114188936")
            event["state"] = "inProgress"
            body["data"]["schedule"]["events"] = [event]
        elif endpoint == "getEventDetails":
            body = read("getEventDetails")
            body["data"]["event"]["match"]["games"][0]["state"] = "inProgress"
        else:
            body = read(endpoint.removesuffix(".json"))
        if "persisted/gw" in path:
            assert request.headers["x-api-key"] == "public-test-key"
        else:
            assert "x-api-key" not in request.headers
        return httpx.Response(200, json=body)


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


async def test_match_uses_real_teams_picks_game_and_feed_clock(tmp_path: Path) -> None:
    replay = LeagueReplay()
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        pack = LeaguePack(http, tmp_path, "public-test-key")
        live = await pack.get_live_match(TITLE)
        assert live is not None
        assert live.game_key == "league:117133830114188936:1"
        assert live.summary == {
            "game": 1,
            "teams": [
                {
                    "name": "RED Kalunga",
                    "picks": ["Rumble", "Wukong", "Orianna", "Xayah", "Rakan"],
                    "bans": None,
                },
                {
                    "name": "FlyQuest",
                    "picks": ["Ornn", "Nocturne", "Syndra", "Yunara", "Lulu"],
                    "bans": None,
                },
            ],
        }
        words = await pack.vocabulary(TITLE, live)
        for term in ("Gryffinn", "zynts", "Massu", "Yunara", "Wukong", "Quad"):
            assert term in words
        assert len(words) <= 350
        assert "Infinity Edge" not in words  # no unrelated item catalogue
        feed = next(r for r in replay.calls if "/window/" in r.url.path)
        assert feed.url.params["startingTime"].endswith("0+00:00")
        prompt = build_prompt(words, "Some earlier speech. " * 80)
        assert prompt.startswith(words)
        assert len(prompt) <= 551


async def test_cache_reuses_real_catalogue_expires_and_recovers_corruption(tmp_path: Path) -> None:
    replay = LeagueReplay(live=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        pack = LeaguePack(http, tmp_path, "public-test-key")
        first = await pack.vocabulary(TITLE, None)
        assert "Gryffinn" in first and "Aegis" in first
        assert len(replay.calls) == 4
        # A fresh Pack instance still reads the disk cache.
        assert await LeaguePack(http, tmp_path, "public-test-key").vocabulary(TITLE, None) == first
        assert len(replay.calls) == 4
        path = tmp_path / "packs/league/versions.json"
        os.utime(path, (0, 0))
        assert DATA_TTL_S == 86400
        assert await pack.vocabulary(TITLE, None) == first
        assert len(replay.calls) == 5
        path.write_text("torn JSON")
        assert await pack.vocabulary(TITLE, None) == first
        assert len(replay.calls) == 6
        path.write_text('{"valid_json": "wrong_schema"}')
        assert await pack.vocabulary(TITLE, None) == first
        assert len(replay.calls) == 7


def test_offline_ambiguous_or_unmentioned_matches_have_no_context() -> None:
    assert select_event(read("getLive"), TITLE) is None
    data = read("getSchedule")
    events = data["data"]["schedule"]["events"]
    selected = next(e for e in events if e.get("match", {}).get("id") == "117133830114188936")
    selected["state"] = "inProgress"
    data["data"]["schedule"]["events"] = [selected]
    assert select_event(data, "playing solo queue") is None
    assert select_event(data, TITLE) == selected
    data["data"]["schedule"]["events"] *= 2
    assert select_event(data, TITLE) is None


def test_malformed_data_fails_at_the_boundary_and_feed_ids_must_match() -> None:
    with pytest.raises(DataError):
        names({"data": {"1": {"name": 12}}})
    assert names({"data": {"1": {"name": ""}, "2": {"name": "Infinity Edge"}}}) == {
        "2": "Infinity Edge"
    }
    event = read("getEventDetails")["data"]["event"]
    event["match"]["games"][0]["state"] = "inProgress"
    window = read("window")
    window["esportsGameId"] = "wrong"
    with pytest.raises(DataError, match="feed game does not match"):
        match_context(event, window, names(read("champion")))


@pytest.mark.parametrize(
    "fail,mode",
    [
        (None, "watching_pro"),
        (None, "playing"),
        ("versions", "watching_pro"),
        ("getLive", "watching_pro"),
        ("getTeams", "watching_pro"),
        ("window", "watching_pro"),
    ],
)
async def test_session_sends_pack_context_and_rules_or_survives_http_failure(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    fail: str | None,
    mode: str,
) -> None:
    fake_stream("70")
    monkeypatch.setenv("FAKE_CATEGORY", "League of Legends")
    monkeypatch.setenv("FAKE_TITLE", TITLE)
    await rules.add(
        pool, "league_take", "Does he predict the winner?", threshold=Decimal("0.7"), pack="league"
    )
    await rules.add(
        pool, "dota_take", "Does he predict the winner?", threshold=Decimal("0.7"), pack="dota"
    )
    replay = LeagueReplay(fail=fail)
    prompts: list[str] = []
    states: list[Any] = []

    class RecordingGroq(GroqStub):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            prompts.append(request.content.decode(errors="replace"))
            return super().__call__(request)

    def jev(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        states.append(body["state"])
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    **{
                        k: {"type": "noul", "noul": 0.8}
                        for k in body["questions"]
                        if k.startswith("rule_")
                    },
                    "mode": {"choice": mode, "probabilities": {mode: 0.99}},
                },
                "usage": {"input_tokens": 500},
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(replay)) as api,
        httpx.AsyncClient(transport=httpx.MockTransport(jev)) as jev_http,
    ):
        pack = LeaguePack(api, tmp_path, "public-test-key")
        await watch_until(
            database_url,
            pool,
            tmp_path / "data",
            RecordingGroq(),
            spans_cover(conn, 70_000),
            gate=JevGate(jev_http, Secret("k")),
            select_pack=lambda category: pack if category == "League of Legends" else None,
        )
    assert states and prompts
    assert await query_rows(conn, "SELECT count(*) FROM utterance") != [(0,)]
    assert await query_rows(
        conn, "SELECT r.name FROM gate_decision d JOIN rule r ON r.id=d.rule_id"
    ) == [("league_take",)]
    if fail:
        assert "live" not in states[0]["context"]
        assert all("Gryffinn" not in p for p in prompts)
        assert await query_rows(conn, "SELECT game_key FROM gate_window") == [(None,)]
    else:
        assert states[0]["context"]["live"]["teams"][1] == {
            "name": "FlyQuest",
            "picks": ["Ornn", "Nocturne", "Syndra", "Yunara", "Lulu"],
            "bans": None,
        }
        assert any("Gryffinn" in p and "Yunara" in p for p in prompts)
        expected_key = "league:117133830114188936:1" if mode == "watching_pro" else None
        assert await query_rows(conn, "SELECT game_key FROM gate_window") == [(expected_key,)]


@pytest.mark.parametrize("change", ["category", "http_failure"])
async def test_refresh_discards_context_after_category_change_or_failure(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    fake_stream("180")
    monkeypatch.setenv("FAKE_HOLD", "1")
    monkeypatch.setenv("FAKE_CATEGORY", "League of Legends")
    monkeypatch.setenv("FAKE_TITLE", TITLE)
    monkeypatch.setattr("kiroku.watch.PACK_REFRESH_S", 0.1)
    await rules.add(pool, "global_take", "Does he say words?", threshold=Decimal("0.7"))
    await rules.add(
        pool, "league_take", "Does he say words?", threshold=Decimal("0.7"), pack="league"
    )
    replay = LeagueReplay()
    changed = False

    async def done() -> bool:
        nonlocal changed
        windows = await query_rows(conn, "SELECT count(*) FROM gate_window")
        if windows[0][0] >= 1 and not changed:
            if change == "category":
                monkeypatch.setenv("FAKE_CATEGORY", "Just Chatting")
            else:
                replay.fail = "getLive"
            changed = True
        return windows[0][0] >= 3

    from tests.test_live_replay import jev_stub

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(replay)) as api,
        httpx.AsyncClient(transport=httpx.MockTransport(jev_stub)) as jev,
    ):
        pack = LeaguePack(api, tmp_path, "public-test-key")
        await watch_until(
            database_url,
            pool,
            tmp_path / "data",
            GroqStub(),
            done,
            gate=JevGate(jev, Secret("k")),
            select_pack=lambda category: pack if category == "League of Legends" else None,
        )
    windows = await query_rows(conn, "SELECT state FROM gate_window ORDER BY start_ms")
    assert windows[0][0]["context"]["live"]["teams"][0]["name"] == "RED Kalunga"
    assert "live" not in windows[2][0]["context"]
    decisions = await query_rows(
        conn,
        """
        SELECT w.start_ms, r.name FROM gate_decision d
        JOIN gate_window w ON w.id=d.window_id JOIN rule r ON r.id=d.rule_id
        WHERE r.pack='league' ORDER BY w.start_ms
    """,
    )
    assert decisions == (
        [(0, "league_take"), (45_000, "league_take")]
        if change == "category"
        else [(0, "league_take"), (45_000, "league_take"), (90_000, "league_take")]
    )


@pytest.mark.parametrize("restart_live", [False, True])
async def test_recovery_uses_historical_pack_and_omits_new_live_context_from_old_windows(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    restart_live: bool,
) -> None:
    from datetime import UTC, datetime

    from kiroku import segments, sessions, transcript
    from kiroku.sessions import claim_channels
    from kiroku.source import StreamMeta
    from kiroku.transcriber import Result, Utterance
    from tests.test_live_replay import jev_stub

    fake_stream("70")
    meta = StreamMeta("broadcast-1", TITLE, "League of Legends")
    async with claim_channels(database_url, "twitch", ["fake"]) as (channel,):
        session = await sessions.open_session(pool, channel, meta, datetime.now(UTC))
        leg = await sessions.next_leg(pool, channel, session)
        await sessions.record_leg(pool, leg, datetime.now(UTC))
        await sessions.end_leg(pool, leg, 90_000)
    await segments.record(pool, session, 0, meta, "league")
    await segments.record(
        pool, session, 45_000, StreamMeta("broadcast-1", "chatting", "Just Chatting"), None
    )
    await transcript.commit(
        pool,
        session,
        "groq:whisper-large-v3-turbo",
        Result(
            0,
            90_000,
            (
                Utterance(0, 10_000, "Yunara is broken."),
                Utterance(45_000, 55_000, "Talking with chat."),
            ),
            None,
        ),
    )
    await rules.add(pool, "global_take", "Does he say words?", threshold=Decimal("0.7"))
    await rules.add(
        pool, "league_take", "Does he say words?", threshold=Decimal("0.7"), pack="league"
    )
    if restart_live:
        monkeypatch.setenv("FAKE_CATEGORY", "League of Legends")
        monkeypatch.setenv("FAKE_TITLE", TITLE)
    else:
        monkeypatch.delenv("FAKE_STREAM_ID")

    async def done() -> bool:
        windows = await query_rows(conn, "SELECT count(*) FROM gate_window")
        return windows[0][0] >= (3 if restart_live else 2)

    replay = LeagueReplay()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(replay)) as api,
        httpx.AsyncClient(transport=httpx.MockTransport(jev_stub)) as jev,
    ):
        pack = LeaguePack(api, tmp_path, "public-test-key")
        await watch_until(
            database_url,
            pool,
            tmp_path / "data",
            GroqStub(),
            done,
            gate=JevGate(jev, Secret("k")),
            select_pack=lambda category: pack if category == "League of Legends" else None,
        )
    windows = await query_rows(conn, "SELECT state,game_key FROM gate_window ORDER BY start_ms")
    assert [w[0]["context"]["stream"]["category"] for w in windows[:2]] == [
        "League of Legends",
        "Just Chatting",
    ]
    assert all("live" not in w[0]["context"] and w[1] is None for w in windows[:2])
    decisions = await query_rows(
        conn,
        """
        SELECT w.start_ms FROM gate_decision d JOIN gate_window w ON w.id=d.window_id
        JOIN rule r ON r.id=d.rule_id WHERE r.pack='league' ORDER BY w.start_ms
    """,
    )
    assert [row[0] for row in decisions][:1] == [0]
    assert 45_000 not in [row[0] for row in decisions]
    if restart_live:
        assert windows[2][0]["context"]["live"]["teams"][1]["name"] == "FlyQuest"


async def test_crash_reconciles_future_segment_boundaries(pool: Pool, database_url: str) -> None:
    from datetime import UTC, datetime

    from kiroku import segments, sessions
    from kiroku.sessions import claim_channels
    from kiroku.source import StreamMeta

    league = StreamMeta("crashed", TITLE, "League of Legends")
    chat = StreamMeta("crashed", "chat", "Just Chatting")
    async with claim_channels(database_url, "twitch", ["fake"]) as (channel,):
        session = await sessions.open_session(pool, channel, league, datetime.now(UTC))
    await segments.record(pool, session, 0, league, "league")
    await segments.record(pool, session, 90_000, chat, None)
    # The capture leg never committed its end. Resume at the committed transcript position.
    await segments.record(pool, session, 45_000, chat, None, restart=True)
    assert await segments.at(pool, session, 60_000) == segments.Segment(
        "chat", "Just Chatting", None
    )
    # Future metadata cannot override a subsequent change after restart.
    await segments.record(pool, session, 70_000, league, "league")
    assert await segments.at(pool, session, 100_000) == segments.Segment(
        TITLE, "League of Legends", "league"
    )


@pytest.mark.parametrize("revert", [False, True])
async def test_metadata_write_retry_keeps_the_first_observed_audio_boundary(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    revert: bool,
) -> None:
    import psycopg

    from kiroku import segments
    from kiroku.source import StreamMeta
    from tests.test_live_replay import jev_stub

    fake_stream("180")
    monkeypatch.setenv("FAKE_HOLD", "1")
    monkeypatch.setenv("FAKE_CATEGORY", "League of Legends")
    monkeypatch.setenv("FAKE_TITLE", TITLE)
    original = segments.record
    boundaries: list[int] = []

    async def flaky(
        pool: Pool,
        session_id: int,
        start_ms: int,
        meta: StreamMeta,
        pack: str | None,
        *,
        restart: bool = False,
    ) -> None:
        if meta.category == "Just Chatting":
            boundaries.append(start_ms)
            if len(boundaries) <= 2:
                if revert:
                    monkeypatch.setenv("FAKE_CATEGORY", "League of Legends")
                raise psycopg.OperationalError("simulated Segment write failure")
        await original(pool, session_id, start_ms, meta, pack, restart=restart)

    monkeypatch.setattr(segments, "record", flaky)
    changed = False

    async def done() -> bool:
        nonlocal changed
        found = await query_rows(conn, "SELECT count(*) FROM gate_window")
        if found[0][0] >= 1 and not changed:
            changed = True
            monkeypatch.setenv("FAKE_CATEGORY", "Just Chatting")
        return found[0][0] >= 3

    async with httpx.AsyncClient(transport=httpx.MockTransport(jev_stub)) as http:
        await watch_until(
            database_url, pool, tmp_path / "data", GroqStub(), done, gate=JevGate(http, Secret("k"))
        )
    assert len(boundaries) == 3 and len(set(boundaries)) == 1
    rows = await query_rows(conn, "SELECT start_ms FROM segment WHERE category='Just Chatting'")
    assert rows == [(boundaries[0],)]
