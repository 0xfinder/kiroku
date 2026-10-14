"""Recorded lolesports + Data Dragon -> Pack -> live transcript -> Gate -> Postgres."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg import AsyncConnection

from kiroku import rules
from kiroku.audio import BYTES_PER_SAMPLE, SAMPLES_PER_MS
from kiroku.config import Secret
from kiroku.db import Pool, open_pool
from kiroku.gate.jev import JevGate
from kiroku.pack import LiveContext, pack_selector
from kiroku.packs.league import LeaguePack, vocabulary
from kiroku.packs.league.data import (
    DATA_TTL_S,
    DataError,
    LeagueData,
    match_context,
    names,
    roster_terms,
    select_event,
)
from kiroku.source import Capture, StreamlinkSource
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
        if self.fail == "window_204" and "/window/" in path:
            return httpx.Response(204)
        if self.fail == "window_empty" and "/window/" in path:
            return httpx.Response(200)
        if self.fail == "window_bad_query" and "/window/" in path:
            return httpx.Response(
                400,
                json={
                    "errorCode": "BAD_QUERY_PARAMETER",
                    "message": "disallowed window with end time less than 80 sec old",
                },
            )
        if self.fail == "getLive_403" and endpoint == "getLive":
            return httpx.Response(403)
        if self.fail and self.fail in path:
            return httpx.Response(503)
        if "/window/" in path:
            body = read("window")
            if self.fail == "window_metadata":
                del body["gameMetadata"]
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


def hold_audio(
    monkeypatch: pytest.MonkeyPatch, *at_ms: int, timeout_s: float = 15
) -> list[tuple[asyncio.Event, asyncio.Event]]:
    """Pause real ffmpeg PCM at exact capture positions until the test releases it."""
    original = StreamlinkSource.capture
    barriers = [(asyncio.Event(), asyncio.Event()) for _ in at_ms]

    async def read(capture: Capture) -> AsyncIterator[bytes]:
        delivered = 0
        step = 0
        async for block in capture.pcm:
            while step < len(at_ms):
                boundary = at_ms[step] * SAMPLES_PER_MS * BYTES_PER_SAMPLE
                if delivered + len(block) < boundary:
                    break
                prefix = boundary - delivered
                if prefix:
                    yield block[:prefix]
                block = block[prefix:]
                delivered = boundary
                reached, resume = barriers[step]
                reached.set()
                try:
                    await asyncio.wait_for(resume.wait(), timeout_s)
                except TimeoutError:
                    raise AssertionError(
                        f"audio barrier at {at_ms[step]} ms was not released"
                    ) from None
                step += 1
            if block:
                yield block
                delivered += len(block)

    class HeldCapture:
        def __init__(self, inner: Capture) -> None:
            self.inner = inner

        @property
        def pcm(self) -> AsyncIterator[bytes]:
            return read(self.inner)

        async def stop_input(self) -> None:
            release_all()
            await self.inner.stop_input()

    def release_all() -> None:
        for _, resume in barriers:
            resume.set()

    @asynccontextmanager
    async def capture(source: StreamlinkSource, archive: Path) -> AsyncGenerator[Capture]:
        async with original(source, archive) as inner:
            try:
                yield HeldCapture(inner)
            finally:
                release_all()

    monkeypatch.setattr(StreamlinkSource, "capture", capture)
    return barriers


@pytest.mark.parametrize("finish", ["timeout", "stop", "body_failure"])
async def test_audio_barrier_deadline_and_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    pcm = b"\0" * (30 * SAMPLES_PER_MS * BYTES_PER_SAMPLE)

    class FakeCapture:
        @property
        def pcm(self) -> AsyncIterator[bytes]:
            async def blocks() -> AsyncIterator[bytes]:
                yield pcm

            return blocks()

        async def stop_input(self) -> None:
            assert all(resume.is_set() for _, resume in barriers)

    @asynccontextmanager
    async def fake_capture(source: StreamlinkSource, archive: Path) -> AsyncGenerator[Capture]:
        try:
            yield FakeCapture()
        finally:
            assert all(resume.is_set() for _, resume in barriers)

    monkeypatch.setattr(StreamlinkSource, "capture", fake_capture)
    barriers = hold_audio(monkeypatch, 10, 20, timeout_s=0.1 if finish == "timeout" else 15)
    source = StreamlinkSource("fake")

    async def consume() -> None:
        async with source.capture(tmp_path / "fake.opus") as capture:
            blocks = capture.pcm
            assert await anext(blocks) == pcm[: len(pcm) // 3]
            pending = asyncio.ensure_future(anext(blocks))
            try:
                await asyncio.wait_for(barriers[0][0].wait(), 1)
                assert not pending.done()
                assert not barriers[1][0].is_set()
                if finish == "timeout":
                    with pytest.raises(
                        AssertionError, match="audio barrier at 10 ms was not released"
                    ):
                        await pending
                elif finish == "stop":
                    await capture.stop_input()
                    assert await asyncio.wait_for(pending, 1) == pcm[: len(pcm) // 3]
                    assert await anext(blocks) == pcm[: len(pcm) // 3]
                else:
                    raise RuntimeError("simulated test body failure")
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    if finish == "body_failure":
        with pytest.raises(RuntimeError, match="simulated test body failure"):
            await consume()
    else:
        await consume()
    assert all(resume.is_set() for _, resume in barriers)


@pytest.fixture
async def pool(database_url: str) -> AsyncGenerator[Pool]:
    async with open_pool(database_url) as p:
        yield p


async def test_match_uses_real_teams_picks_game_and_feed_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 14, 12, 34, 57, 123456, tzinfo=UTC)

    class FrozenClock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            assert tz == UTC
            return now

    monkeypatch.setattr("kiroku.packs.league.datetime", FrozenClock)
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
        at = datetime.fromisoformat(feed.url.params["startingTime"])
        assert now - timedelta(seconds=110) <= at <= now - timedelta(seconds=90)
        assert at.second % 10 == 0 and at.microsecond == 0
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


def test_title_fallback_keeps_the_last_word_and_truncates_at_a_word_boundary() -> None:
    assert vocabulary("T1 vs Gen.G LCK Finals", {}, {}, (), None) == (
        "League of Legends. T1 vs Gen.G LCK Finals"
    )
    assert vocabulary("Finals " * 60, {}, {}, (), None) == (
        "League of Legends. " + " ".join(["Finals"] * 47)
    )
    assert vocabulary("x" * 400, {}, {}, (), None) == "League of Legends."
    assert vocabulary("x" * 331 + " Finals", {}, {}, (), None) == (
        "League of Legends. " + "x" * 331
    )


def test_catalogue_terms_match_whole_words_in_the_title() -> None:
    champions = {"Vi": "Vi"}
    items = {"1": "Infinity Edge"}
    assert vocabulary("VOD REVIEW with viewers", champions, items, (), None) == (
        "League of Legends. VOD REVIEW with viewers"
    )
    assert vocabulary("VI: infinity EDGE!", champions, items, (), None) == (
        "League of Legends. Vi, Infinity Edge, VI: infinity EDGE!"
    )


def test_rosters_skip_players_without_usable_names() -> None:
    data = {
        "data": {
            "teams": [
                {
                    "status": "active",
                    "code": "T1",
                    "players": [
                        {"summonerName": "Faker", "role": "mid"},
                        {"summonerName": "", "role": "top"},
                        {"role": "jungle"},
                        {"summonerName": "coach", "role": "coach"},
                    ],
                }
            ]
        }
    }
    assert roster_terms(data, "T1 Finals") == ("Faker",)


@pytest.mark.parametrize("failure", ["http", "invalid"])
async def test_stale_cache_survives_failed_refresh_and_keeps_retrying(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    replay = LeagueReplay(live=False)

    def api(request: httpx.Request) -> httpx.Response:
        if replay.fail and failure == "invalid":
            replay.calls.append(request)
            return httpx.Response(200, json={"wrong": "shape"})
        return replay(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as http:
        pack = LeaguePack(http, tmp_path, "public-test-key")
        first = await pack.vocabulary(TITLE, None)
        path = tmp_path / "packs/league/versions.json"
        os.utime(path, (0, 0))
        replay.fail = "versions"
        for expected_calls in (5, 6):
            assert await pack.vocabulary(TITLE, None) == first
            assert len(replay.calls) == expected_calls
            assert path.stat().st_mtime == 0
        assert "stale" in caplog.text
        replay.fail = None
        assert await pack.vocabulary(TITLE, None) == first
        assert len(replay.calls) == 7 and path.stat().st_mtime > 0


async def test_catalogue_memoization_invalidates_when_disk_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = LeagueReplay()
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        pack = LeaguePack(http, tmp_path, "public-test-key")
        await pack.get_live_match(TITLE)
        await pack.vocabulary(TITLE, None)
        original = Path.read_text
        reads: list[str] = []

        def count_reads(path: Path, *args: Any, **kwargs: Any) -> str:
            if path.parent == tmp_path / "packs/league" and path.name != "teams.json":
                reads.append(path.name)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", count_reads)
        await pack.get_live_match(TITLE)
        await pack.vocabulary(TITLE, None)
        assert reads == []
        path = next((tmp_path / "packs/league").glob("champion-*.json"))
        body = json.loads(original(path))
        body["data"]["Yunara"]["name"] = "Updated Yunara"
        path.write_text(json.dumps(body))
        live = await pack.get_live_match(TITLE)
        assert live is not None and "Updated Yunara" in live.vocabulary
        assert reads == [path.name]


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
    "fail,error",
    [
        ("versions", httpx.HTTPStatusError),
        ("window", httpx.HTTPStatusError),
        ("window_metadata", (KeyError, DataError)),
        ("getLive_403", httpx.HTTPStatusError),
    ],
)
async def test_pack_endpoint_failures(
    tmp_path: Path, fail: str, error: type[Exception] | tuple[type[Exception], ...]
) -> None:
    replay = LeagueReplay(fail=fail)
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as api:
        pack = LeaguePack(api, tmp_path, "public-test-key")
        with pytest.raises(error) as exc:
            await pack.get_live_match(TITLE)
        if fail in {"versions", "window", "getLive_403"}:
            assert isinstance(exc.value, httpx.HTTPStatusError)
            expected_url = {
                "versions": "https://ddragon.leagueoflegends.com/api/versions.json",
                "window": "https://feed.lolesports.com/livestats/v1/window/117133830114188937",
                "getLive_403": "https://esports-api.lolesports.com/persisted/gw/getLive",
            }[fail]
            assert exc.value.response.status_code == (403 if fail == "getLive_403" else 503)
            assert str(exc.value.request.url.copy_with(query=None)) == expected_url
        else:
            assert any("/window/" in request.url.path for request in replay.calls)
        if fail == "versions":
            with pytest.raises(httpx.HTTPStatusError):
                await pack.vocabulary(TITLE, None)
        else:
            words = await pack.vocabulary(TITLE, None)
            assert "Gryffinn" in words and "Aegis" in words
            assert len(words) <= 350


@pytest.mark.parametrize("fail", ["window_204", "window_empty", "window_bad_query"])
async def test_feed_without_context_retries_and_preserves_vocabulary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fail: str
) -> None:
    replay = LeagueReplay()
    async with httpx.AsyncClient(transport=httpx.MockTransport(replay)) as http:
        pack = LeaguePack(http, tmp_path, "public-test-key")
        assert await pack.get_live_match(TITLE) is not None
        replay.fail = fail
        for _ in range(3):
            assert await pack.get_live_match(TITLE) is None
            words = await pack.vocabulary(TITLE, None)
            assert "Gryffinn" in words and "Aegis" in words
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        if fail == "window_bad_query":
            assert len(warnings) == 1
            assert "BAD_QUERY_PARAMETER" in warnings[0].getMessage()
            assert warnings[0].exc_info is None
        else:
            assert warnings == []
        replay.fail = None
        assert await pack.get_live_match(TITLE) is not None
        if fail == "window_bad_query":
            replay.fail = fail
            assert await pack.get_live_match(TITLE) is None
            warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert len(warnings) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"errorCode": "BAD_QUERY_PARAMETER", "message": "invalid startingTime format"},
        {"errorCode": "BAD_QUERY_PARAMETER"},
        {"errorCode": "OTHER", "message": "ahead of broadcast"},
        {"errorCode": "BAD_QUERY_PARAMETER", "message": 80},
        [],
        "non-JSON error",
    ],
)
async def test_feed_other_bad_queries_raise(tmp_path: Path, body: object) -> None:
    replay = LeagueReplay()

    def respond(request: httpx.Request) -> httpx.Response:
        if "/window/" in request.url.path:
            if isinstance(body, str):
                return httpx.Response(400, text=body)
            return httpx.Response(400, json=body)
        return replay(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        pack = LeaguePack(http, tmp_path, "public-test-key")
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await pack.get_live_match(TITLE)
        assert exc.value.response.status_code == 400


@pytest.mark.parametrize("success", [None, "window_204", "window_empty"])
async def test_feed_ahead_of_broadcast_warning_resets_after_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, success: str | None
) -> None:
    replay = LeagueReplay()
    rejected = True

    def respond(request: httpx.Request) -> httpx.Response:
        if rejected and "/window/" in request.url.path:
            return httpx.Response(
                400,
                json={"errorCode": "BAD_QUERY_PARAMETER", "message": "ahead of broadcast"},
            )
        return replay(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        pack = LeaguePack(http, tmp_path, "public-test-key")
        for _ in range(2):
            assert await pack.get_live_match(TITLE) is None
        assert len(caplog.records) == 1
        rejected = False
        replay.fail = success
        live = await pack.get_live_match(TITLE)
        assert (live is not None) == (success is None)
        rejected = True
        assert await pack.get_live_match(TITLE) is None
        assert len(caplog.records) == 2
        assert all("BAD_QUERY_PARAMETER" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status", [200, 204])
async def test_league_shared_get_still_rejects_empty_bodies(tmp_path: Path, status: int) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as http:
        data = LeagueData(http, tmp_path, "public-test-key")
        with pytest.raises(ValueError):
            await data.get("https://example.test/catalogue")


@pytest.mark.parametrize(
    "fail,mode",
    [
        (None, "watching_pro"),
        (None, "playing"),
        ("getLive", "watching_pro"),
        ("getTeams", "watching_pro"),
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
            select_pack=pack_selector([pack]),
        )
    assert states and prompts
    assert await query_rows(conn, "SELECT count(*) FROM utterance") != [(0,)]
    assert await query_rows(
        conn, "SELECT r.name FROM gate_decision d JOIN rule r ON r.id=d.rule_id"
    ) == [("league_take",)]
    if fail == "getLive":
        assert "live" not in states[0]["context"]
        assert await query_rows(conn, "SELECT game_key FROM gate_window") == [(None,)]
    else:
        assert states[0]["context"]["live"]["teams"][1] == {
            "name": "FlyQuest",
            "picks": ["Ornn", "Nocturne", "Syndra", "Yunara", "Lulu"],
            "bans": None,
        }
        expected_key = "league:117133830114188936:1" if mode == "watching_pro" else None
        assert await query_rows(conn, "SELECT game_key FROM gate_window") == [(expected_key,)]
    if fail == "getTeams":
        assert all("Gryffinn" not in p for p in prompts)
    elif fail == "getLive":
        assert any("Gryffinn" in p and "Aegis" in p for p in prompts)
    else:
        assert any("Gryffinn" in p and "Yunara" in p for p in prompts)


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
    fake_stream("150")
    monkeypatch.setenv("FAKE_HOLD", "1")
    monkeypatch.setenv("FAKE_PACE", "40")
    monkeypatch.setenv("FAKE_CATEGORY", "League of Legends")
    monkeypatch.setenv("FAKE_TITLE", TITLE)
    monkeypatch.setattr("kiroku.watch.PACK_REFRESH_S", 0.1)
    await rules.add(pool, "global_take", "Does he say words?", threshold=Decimal("0.7"))
    await rules.add(
        pool, "league_take", "Does he say words?", threshold=Decimal("0.7"), pack="league"
    )
    replay = LeagueReplay()
    initial, changed_audio = hold_audio(monkeypatch, 0, 65_000)
    changed = False

    class ControlledPack(LeaguePack):
        live_unavailable = False

        async def get_live_match(self, title: str) -> LiveContext | None:
            self.live_unavailable = False
            try:
                live = await super().get_live_match(title)
            except httpx.HTTPError:
                self.live_unavailable = True
                raise
            self.live_unavailable = live is None
            return live

        async def vocabulary(self, title: str, live: LiveContext | None) -> str:
            words = await super().vocabulary(title, live)
            initial[1].set()
            if changed and self.live_unavailable:
                changed_audio[1].set()
            return words

    async def done() -> bool:
        nonlocal changed
        windows = await query_rows(conn, "SELECT count(*) FROM gate_window")
        if windows[0][0] >= 1 and changed_audio[0].is_set() and not changed:
            if change == "category":
                monkeypatch.setenv("FAKE_CATEGORY", "Just Chatting")
            else:
                replay.fail = "getLive"
            changed = True
        if change == "category" and changed and not changed_audio[1].is_set():
            segments = await query_rows(
                conn, "SELECT start_ms FROM segment WHERE category='Just Chatting'"
            )
            if segments:
                assert segments == [(65_000,)]
                changed_audio[1].set()
        return windows[0][0] >= 3

    from tests.test_live_replay import jev_stub

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(replay)) as api,
        httpx.AsyncClient(transport=httpx.MockTransport(jev_stub)) as jev,
    ):
        pack = ControlledPack(api, tmp_path, "public-test-key")
        await watch_until(
            database_url,
            pool,
            tmp_path / "data",
            GroqStub(),
            done,
            gate=JevGate(jev, Secret("k")),
            select_pack=pack_selector([pack]),
            poll_s=0.1,
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
            select_pack=pack_selector([pack]),
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

    fake_stream("150")
    monkeypatch.setenv("FAKE_HOLD", "1")
    monkeypatch.setenv("FAKE_PACE", "40")
    monkeypatch.setenv("FAKE_CATEGORY", "League of Legends")
    monkeypatch.setenv("FAKE_TITLE", TITLE)
    barriers = hold_audio(monkeypatch, 65_000, 75_000, 85_000)
    original = segments.record
    boundaries: list[int] = []
    attempts = (
        (barriers[0][1], barriers[1][0], 75_000),
        (barriers[1][1], barriers[2][0], 85_000),
        (barriers[2][1], None, None),
    )

    async def flaky(
        pool: Pool,
        session_id: int,
        start_ms: int,
        meta: StreamMeta,
        pack: str | None,
        *,
        restart: bool = False,
    ) -> None:
        resume = None
        if meta.category == "Just Chatting":
            assert len(boundaries) < len(attempts), (
                f"unexpected Segment write attempt {len(boundaries) + 1} at {start_ms} ms; "
                f"expected {len(attempts)} attempts"
            )
            resume, reached, next_ms = attempts[len(boundaries)]
            boundaries.append(start_ms)
            if reached is not None:
                if revert:
                    monkeypatch.setenv("FAKE_CATEGORY", "League of Legends")
                resume.set()
                try:
                    await asyncio.wait_for(reached.wait(), 15)
                except TimeoutError:
                    raise AssertionError(f"audio barrier at {next_ms} ms was not reached") from None
                raise psycopg.OperationalError("simulated Segment write failure")
        await original(pool, session_id, start_ms, meta, pack, restart=restart)
        if resume is not None:
            resume.set()

    monkeypatch.setattr(segments, "record", flaky)
    changed = False

    async def done() -> bool:
        nonlocal changed
        found = await query_rows(conn, "SELECT count(*) FROM gate_window")
        if found[0][0] >= 1 and barriers[0][0].is_set() and not changed:
            changed = True
            monkeypatch.setenv("FAKE_CATEGORY", "Just Chatting")
        return found[0][0] >= 3

    async with httpx.AsyncClient(transport=httpx.MockTransport(jev_stub)) as http:
        await watch_until(
            database_url,
            pool,
            tmp_path / "data",
            GroqStub(),
            done,
            gate=JevGate(http, Secret("k")),
            poll_s=0.1,
        )
    assert boundaries == [65_000, 65_000, 65_000]
    rows = await query_rows(conn, "SELECT start_ms FROM segment WHERE category='Just Chatting'")
    assert rows == [(boundaries[0],)]
