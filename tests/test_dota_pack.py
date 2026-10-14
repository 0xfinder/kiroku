"""Recorded free data -> Dota Vocabulary and the CLI's Category composition."""

import asyncio
import json
import os
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection

from kiroku import rules
from kiroku.cli import PACK_FACTORIES
from kiroku.config import Secret, Settings
from kiroku.db import Pool
from kiroku.gate.jev import JevGate
from kiroku.pack import NotesPack, pack_selector
from kiroku.packs.common import DATA_TTL_S, DataError, text
from kiroku.packs.dota import VOCABULARY_CHARS, DotaPack, vocabulary
from kiroku.packs.dota.data import (
    API,
    TEAM_RECENCY_S,
    DotaData,
    Team,
    heroes,
    items,
    players,
    team_index,
    teams,
    title_teams,
)
from kiroku.transcriber.groq import build_prompt
from tests.test_live_replay import GroqStub, query_rows, spans_cover, watch_until
from tests.test_live_replay import fake_stream as fake_stream
from tests.test_live_replay import pool as pool

FIXTURES = Path(__file__).parent / "fixtures" / "dota"
TITLE = "(EN) Spirit vs Yandex - GRAND FINALS! - Blast Slam VIII - Official co-stream w/ PLD"
RECORDED_NOW = 1791936000


@pytest.fixture(autouse=True)
def recorded_team_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Freeze team recency without changing the cache or watcher wall clock.
    monkeypatch.setattr("kiroku.packs.dota.time", SimpleNamespace(time=lambda: RECORDED_NOW))


def read(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class Replay:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.malformed = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert "x-api-key" not in request.headers
        path = request.url.path
        self.calls.append(path)
        if self.fail:
            return httpx.Response(503)
        name = (
            f"players-{path.split('/')[-2]}"
            if path.endswith("/players")
            else path.rsplit("/", 1)[-1].removesuffix(".json")
        )
        return httpx.Response(200, json={} if self.malformed else read(name))


async def test_recorded_vocabulary_and_composition(tmp_path: Path) -> None:
    replay = Replay()
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        settings = Settings.load(
            {"KIROKU_DATA_DIR": str(tmp_path), "KIROKU_SECRETS_FILE": str(tmp_path / "absent.env")}
        )
        packs = [factory(http, settings) for factory in PACK_FACTORIES.values()]
        pack = pack_selector(packs)("Dota 2")
        assert isinstance(pack, DotaPack)
        assert not isinstance(pack, NotesPack)
        assert await pack.get_live_match(TITLE) is None
        words = await pack.vocabulary(TITLE, None)
        method = json.loads(
            (
                Path(__file__).parents[1] / "spikes/results/2026-10-14-dota-vocabulary/method.json"
            ).read_text()
        )
        assert words == method["vocabulary"]["rosters"]
        for term in ("Yatoro", "Larl", "AMMAR_THE_F", "医者watson`", "Maladych", "DM"):
            assert term in words
        for term in ("Noticed", "Bane", "Black King Bar", " ATF,", " Ammar,", " Malady,"):
            assert term not in words
        assert len(words) <= VOCABULARY_CHARS
        assert len(build_prompt(words, "previous speech " * 80)) <= 551
        assert len(replay.calls) == 5
        assert await DotaPack(http, tmp_path).vocabulary(TITLE, None) == words
        assert len(replay.calls) == 5
        unrelated = await pack.vocabulary("Pub games: Kez and Black King Bar", None)
        assert "Kez," in unrelated and "Black King Bar," in unrelated
        assert "Yatoro" not in unrelated and "Ammar" not in unrelated
        assert len(replay.calls) == 5


def test_title_matching_recency_and_duplicates() -> None:
    catalogue = teams(read("teams"))
    assert [t.id for t in title_teams(catalogue, TITLE, RECORDED_NOW)] == [7119388, 9823272]
    assert [t.id for t in title_teams(catalogue, "TEAM SPIRIT vs TY", RECORDED_NOW)] == [
        7119388,
        9823272,
    ]
    assert not title_teams(catalogue, "Earthspirit pub games", RECORDED_NOW)
    assert not title_teams(catalogue, "Spiritual Yandexing", RECORDED_NOW)
    assert not title_teams(catalogue, TITLE, RECORDED_NOW + TEAM_RECENCY_S)
    duplicate = Team(10, "Team. Spirit", "TSpirit", RECORDED_NOW - 100)
    assert [t.id for t in title_teams((*catalogue, duplicate), "Team Spirit", RECORDED_NOW)] == [10]
    assert not title_teams(catalogue, "Spirit", 0)  # future data


def test_selection_budget_and_word_boundaries() -> None:
    assert vocabulary("VOD REVIEW", (), ("Io", "Drow Ranger")) == "Dota 2. VOD REVIEW"
    assert (
        vocabulary("IO with DROW RANGER", (), ("Io", "Drow Ranger"))
        == "Dota 2. Io, Drow Ranger, IO with DROW RANGER"
    )
    assert vocabulary("x" * 400, (), ()) == "Dota 2."
    words = vocabulary("Finals " * 100, ("Ammar", "ATF", "Ammar"), ("unrelated",))
    assert words.startswith("Dota 2. Ammar, ATF,")
    assert words.count("Ammar") == 1
    assert len(words) <= VOCABULARY_CHARS and words.endswith("Finals")
    crowded = vocabulary("Kez", tuple(f"player{i}" for i in range(100)), ("Kez",))
    assert len(crowded) <= VOCABULARY_CHARS and "player0" in crowded


def test_real_shapes_skip_internal_items_and_historical_players() -> None:
    assert len(heroes(read("heroes"))) == 127
    assert len(items(read("items"))) == 491
    yandex = players(read("players-9823272"))
    assert len(yandex) == 6
    assert {p.name for p in yandex} >= {"DM", "AMMAR_THE_F"}
    assert len(players(read("players-7119388"))) == 5


@pytest.mark.parametrize(
    ("parse", "body"),
    [
        (heroes, []),
        (heroes, {"x": {"localized_name": 1}}),
        (items, {"x": {"dname": False}}),
        (teams, {}),
        (teams, [{"team_id": True, "name": "Spirit", "tag": "", "last_match_time": 1}]),
        (players, [{"account_id": 1, "name": "ATF", "is_current_team_member": "true"}]),
        (players, [{"account_id": 1, "name": 3, "is_current_team_member": True}]),
    ],
)
def test_invalid_shapes(parse: Any, body: object) -> None:
    with pytest.raises((DataError, KeyError)):
        parse(body)


async def test_stale_failure_retries_without_renewing_and_validates_before_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1000.0
    monkeypatch.setattr(
        "kiroku.packs.common.time", SimpleNamespace(time=time.time, monotonic=lambda: now)
    )
    replay = Replay()
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        pack = DotaPack(http, tmp_path)
        first = await pack.vocabulary(TITLE, None)
        paths = list((tmp_path / "packs/dota").glob("*.json"))
        for path in paths:
            os.utime(path, (0, 0))
        old = {path: path.read_bytes() for path in paths}
        replay.malformed = True
        assert await pack.vocabulary(TITLE, None) == first
        for path in paths:
            assert path.stat().st_mtime == 0 and path.read_bytes() == old[path]
        calls = len(replay.calls)
        replay.fail = True
        assert await pack.vocabulary(TITLE, None) == first
        assert len(replay.calls) == calls
        assert "using stale cache" in caplog.text
        assert DATA_TTL_S == 86400
        cold = DotaPack(http, tmp_path / "cold")
        assert await cold.vocabulary(TITLE, None) == "Dota 2. " + TITLE
        assert not cold.data.cache_dir.exists()
        replay.fail = replay.malformed = False
        now += 300
        assert await pack.vocabulary(TITLE, None) == first
        assert all(path.stat().st_mtime > 0 for path in paths)
        paths[0].write_text("torn JSON")
        assert await pack.vocabulary(TITLE, None) == first


async def test_mtime_memoization_and_shape_changes(tmp_path: Path) -> None:
    calls = 0

    def parse(body: object) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        return heroes(body)

    replay = Replay()
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        data = DotaData(http, tmp_path)
        first = await data.cached("heroes", f"{API}/heroes.json", parse)
        assert await data.cached("heroes", f"{API}/heroes.json", parse) == first
        assert calls == 1 and len(replay.calls) == 1
        path = data.cache_dir / "heroes.json"
        stamp = path.stat().st_mtime_ns
        path.write_text(json.dumps({"1": {"localized_name": "Kez"}}))
        os.utime(path, ns=(stamp + 1_000_000, stamp + 1_000_000))
        assert await data.cached("heroes", f"{API}/heroes.json", parse) == ("Kez",)
        assert calls == 2
        path.write_text("{}")
        os.utime(path, ns=(stamp + 2_000_000, stamp + 2_000_000))
        assert await data.cached("heroes", f"{API}/heroes.json", parse) == first
        assert len(replay.calls) == 2


@pytest.mark.parametrize("failed", [False, True])
async def test_watch_uses_dota_vocabulary_without_live_context_and_survives_outage(
    database_url: str,
    pool: Pool,
    conn: AsyncConnection,
    tmp_path: Path,
    fake_stream: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
) -> None:
    fake_stream("70")
    monkeypatch.setenv("FAKE_CATEGORY", "Dota 2")
    monkeypatch.setenv("FAKE_TITLE", TITLE)
    await rules.add(
        pool, "dota_take", "Does he predict the winner?", threshold=Decimal("0.7"), pack="dota"
    )
    await rules.add(
        pool, "league_take", "Does he predict the winner?", threshold=Decimal("0.7"), pack="league"
    )
    replay = Replay()
    replay.fail = failed
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
                    "mode": {"choice": "watching_pro", "probabilities": {"watching_pro": 0.99}},
                },
                "usage": {"input_tokens": 500},
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(replay)) as api,
        httpx.AsyncClient(transport=httpx.MockTransport(jev)) as jev_http,
    ):
        pack = DotaPack(api, tmp_path)
        await watch_until(
            database_url,
            pool,
            tmp_path / "data",
            RecordingGroq(),
            spans_cover(conn, 70_000),
            gate=JevGate(jev_http, Secret("k")),
            select_pack=pack_selector([pack]),
        )
    assert states and prompts
    assert all("live" not in state["context"] for state in states)
    assert await query_rows(conn, "SELECT game_key FROM gate_window") == [(None,)]
    assert await query_rows(conn, "SELECT pack FROM segment") == [("dota",)]
    assert await query_rows(
        conn, "SELECT r.name FROM gate_decision d JOIN rule r ON r.id=d.rule_id"
    ) == [("dota_take",)]
    assert any(TITLE in prompt for prompt in prompts)
    assert any("Yatoro," in prompt for prompt in prompts) is (not failed)
    assert await query_rows(conn, "SELECT count(*) FROM utterance") != [(0,)]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("MMR grind, alt account later", []),
        ("MMR grind - alt account later", []),
        ("13K MMR GRIND - ALT ACCOUNT", []),
        ("Gorgc | Re: watching OG vs Tundra", [2586976, 8291895]),
        (TITLE, [7119388, 9823272]),
        ("(EN) Tundra x OG - BLAST co-stream with PLD", [8291895, 2586976]),
        ("Team Spirit - Team Yandex | GRAND FINALS", [7119388, 9823272]),
        ("Team Liquid highlights | Spirit vs Yandex", [7119388, 9823272]),
        ("13K MMR pubs | alt account | back to the grind", []),
        ("Gorgc | Spirit Breaker pub games, watching replays later", []),
        ("Ranked games with PLD | OG later", []),
        ("Team Spirit highlights with PLD", [7119388]),
        ("Spirit vs ty", [7119388]),
    ],
)
def test_real_catalogue_title_selection(title: str, expected: list[int]) -> None:
    catalogue = teams(read("teams"))
    assert [team.id for team in title_teams(catalogue, title, RECORDED_NOW)] == expected
    assert len(title_teams(catalogue, title, RECORDED_NOW)) <= 2


def test_real_catalogue_skips_bad_rows_and_reuses_index() -> None:
    rows = read("teams")
    assert len(rows) == 1000
    assert sum(not row["name"].strip() for row in rows) == 17
    catalogue = teams(rows)
    assert len(catalogue) == 983
    assert teams([*rows, {"name": "Missing ID"}, {"team_id": 1, "name": " "}]) == catalogue
    assert team_index(catalogue) is team_index(teams(rows))
    assert any(row.get("dname") == " " for row in read("items").values())
    assert items({"blank": {"dname": " "}, "named": {"dname": " Kez "}}) == ("Kez",)


async def test_hanging_fetches_use_stale_cache_and_sources_run_independently(
    tmp_path: Path,
) -> None:
    replay = Replay()
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        first = await DotaPack(http, tmp_path).vocabulary(TITLE, None)
    paths = list((tmp_path / "packs/dota").glob("*.json"))
    for path in paths:
        os.utime(path, (0, 0))
    entered: set[str] = set()
    catalogue_started = asyncio.Event()
    rosters_started = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        entered.add(request.url.path)
        if request.url.path.endswith("items.json"):
            catalogue_started.set()
        if sum(path.endswith("/players") for path in entered) == 2:
            rosters_started.set()
        await asyncio.Future[None]()
        raise AssertionError("hung request unexpectedly returned")

    async with httpx.AsyncClient(transport=httpx.MockTransport(hang)) as http:
        pack = DotaPack(http, tmp_path)
        pack.data.fetch_timeout_s = 0.05
        # Teams then parallel rosters take at most two fetch deadlines, independent
        # of the stable catalogue's parallel requests. The outer timeout catches hangs.
        async with asyncio.timeout(0.5):
            task = asyncio.create_task(pack.vocabulary(TITLE, None))
            await catalogue_started.wait()
            await rosters_started.wait()
            assert await task == first
        assert len(entered) == 5
        assert await pack.vocabulary(TITLE, None) == first
        assert len(entered) == 5
        assert all(path.stat().st_mtime == 0 for path in paths)
        cold = DotaPack(http, tmp_path / "cold")
        cold.data.fetch_timeout_s = 0.05
        async with asyncio.timeout(0.5):
            assert await cold.vocabulary("Pub games with Kez", None) == "Dota 2. Pub games with Kez"
        assert not cold.data.cache_dir.exists()


async def test_opendota_hang_preserves_fresh_catalogue_on_cold_start(tmp_path: Path) -> None:
    async def api(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.opendota.com":
            await asyncio.Future[None]()
        return Replay()(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        pack = DotaPack(http, tmp_path)
        pack.data.fetch_timeout_s = 0.05
        async with asyncio.timeout(0.5):
            assert await pack.vocabulary("Pub games: Kez and Black King Bar", None) == (
                "Dota 2. Kez, Black King Bar, Pub games: Kez and Black King Bar"
            )


async def test_failure_backoff_expires_without_touching_stale_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1000.0
    monkeypatch.setattr(
        "kiroku.packs.common.time", SimpleNamespace(time=time.time, monotonic=lambda: now)
    )
    replay = Replay()
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        data = DotaData(http, tmp_path)
        first = await data.cached("heroes", f"{API}/heroes.json", heroes)
        path = data.cache_dir / "heroes.json"
        os.utime(path, (0, 0))
        replay.fail = True
        assert await data.cached("heroes", f"{API}/heroes.json", heroes) == first
        assert len(replay.calls) == 2
        now += 299
        assert await data.cached("heroes", f"{API}/heroes.json", heroes) == first
        assert len(replay.calls) == 2 and path.stat().st_mtime == 0
        now += 1
        replay.fail = False
        assert await data.cached("heroes", f"{API}/heroes.json", heroes) == first
        assert len(replay.calls) == 3 and path.stat().st_mtime > 0


def test_duplicate_tag_prefers_recent_team_and_preserves_matchup_order() -> None:
    catalogue = (
        Team(1, "First Team", "ABC", RECORDED_NOW - 100),
        Team(2, "Second Team", "ABC", RECORDED_NOW - 10),
        Team(3, "Third Team", "THIRD", RECORDED_NOW - 5),
    )
    assert [
        team.id for team in title_teams(catalogue, "First Team vs Second Team", RECORDED_NOW)
    ] == [2]
    assert [team.id for team in title_teams(catalogue, "ABC vs Third Team", RECORDED_NOW)] == [2, 3]


def test_shared_text_preserves_league_and_trims_dota_explicitly() -> None:
    assert text(" League ") == " League "
    assert text(" ") == " "
    assert text(" Dota ", strip=True) == "Dota"
    with pytest.raises(DataError):
        text(" ", strip=True)
    assert items({"invalid": None, "blank": {"dname": " "}, "valid": {"dname": " Kez "}}) == (
        "Kez",
    )
