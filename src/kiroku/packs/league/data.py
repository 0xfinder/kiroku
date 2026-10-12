"""Free League HTTP data, with a disk TTL cache and typed JSON parsing."""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import httpx

from kiroku.gate.windows import JSON

DDRAGON = "https://ddragon.leagueoflegends.com"
PERSISTED = "https://esports-api.lolesports.com/persisted/gw"
FEED = "https://feed.lolesports.com/livestats/v1"
# lolesports.com's public website key, shipped to every browser.
PUBLIC_WEBSITE_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"
DATA_TTL_S = 24 * 3600
log = logging.getLogger(__name__)


class DataError(ValueError):
    pass


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DataError("expected an object")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise DataError("expected an array")
    return cast(list[object], value)


def text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DataError("expected a nonempty string")
    return value


def integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DataError("expected an integer")
    return value


class LeagueData:
    def __init__(self, http: httpx.AsyncClient, data_dir: Path, key: str) -> None:
        self.http = http
        self.cache_dir = data_dir / "packs" / "league"
        self.key = key
        self._lock = asyncio.Lock()
        self._parsed: dict[tuple[str, object], tuple[int, object]] = {}

    async def get(
        self, url: str, *, params: Mapping[str, str] | None = None, website: bool = False
    ) -> object:
        response = await self.http.get(
            url,
            params=params,
            headers={"x-api-key": self.key} if website else {},
            timeout=10,
        )
        response.raise_for_status()
        body: object = response.json()
        return body

    async def persisted(self, endpoint: str, **params: str) -> dict[str, object]:
        return obj(
            obj(
                await self.get(
                    f"{PERSISTED}/{endpoint}", params={"hl": "en-US", **params}, website=True
                )
            )["data"]
        )

    async def cached[T](
        self,
        name: str,
        url: str,
        parse: Callable[[object], T],
        *,
        website: bool = False,
        memoize: bool = False,
    ) -> T:
        async with self._lock:
            path = self.cache_dir / f"{name}.json"
            stale: tuple[T] | None = None
            memo_key = (name, parse)
            try:
                stat = path.stat()
                memo = self._parsed.get(memo_key) if memoize else None
                if memo is not None and memo[0] == stat.st_mtime_ns:
                    parsed = cast(T, memo[1])
                else:
                    cached: object = json.loads(path.read_text())
                    parsed = parse(cached)
                    if memoize:
                        self._parsed[memo_key] = (stat.st_mtime_ns, parsed)
                stale = (parsed,)
                if time.time() - stat.st_mtime < DATA_TTL_S:
                    return parsed
            except (OSError, ValueError, KeyError, IndexError):
                pass
            try:
                body = await self.get(url, website=website)
                parsed = parse(body)
                # Atomic replace also protects another watcher reading the same cache.
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(body))
                temporary.replace(path)
                if memoize:
                    self._parsed[memo_key] = (path.stat().st_mtime_ns, parsed)
            except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as e:
                if stale is None:
                    raise
                log.warning("League %s refresh failed; using stale cache (%s)", name, e)
                return stale[0]
            return parsed


def versions(data: object) -> tuple[str, ...]:
    values = tuple(text(v) for v in array(data))
    if not values:
        raise DataError("expected at least one Data Dragon version")
    return values


def names(data: object) -> dict[str, str]:
    # Data Dragon includes unnamed internal items. They have no spoken Vocabulary.
    return {
        key: text(obj(value)["name"])
        for key, value in obj(obj(data)["data"]).items()
        if obj(value)["name"] != ""
    }


def roster_terms(data: object, title: str) -> tuple[str, ...]:
    teams = [obj(team) for team in array(obj(obj(data)["data"])["teams"])]
    chosen = [team for team in teams if team.get("status") == "active" and mentioned(team, title)]
    return tuple(
        name
        for team in chosen
        for player in array(team["players"])
        if obj(player).get("role") != "coach"
        if isinstance(name := obj(player).get("summonerName"), str) and name.strip()
    )


def contains_term(term: str, title: str) -> bool:
    return bool(term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", title, re.I))


def mentioned(team: dict[str, object], title: str) -> bool:
    return any(
        isinstance(term, str) and contains_term(term, title)
        for term in (team.get("code"), team.get("name"))
    )


def select_event(data: object, title: str) -> dict[str, object] | None:
    events = [obj(e) for e in array(obj(obj(obj(data)["data"])["schedule"])["events"])]
    live = [e for e in events if e.get("type") == "match" and e.get("state") == "inProgress"]
    matched = [
        e for e in live if any(mentioned(obj(t), title) for t in array(obj(e["match"])["teams"]))
    ]
    return matched[0] if len(matched) == 1 else None


def match_context(
    event: object, window: object, champions: Mapping[str, str]
) -> tuple[dict[str, JSON], str, tuple[str, ...]]:
    event = obj(event)
    match = obj(event["match"])
    games = [obj(g) for g in array(match["games"]) if obj(g).get("state") == "inProgress"]
    if len(games) != 1:
        raise DataError("expected one current game")
    game = games[0]
    window = obj(window)
    if window["esportsGameId"] != game["id"] or window["esportsMatchId"] != event["id"]:
        raise DataError("feed game does not match event")
    metadata = obj(window["gameMetadata"])
    teams = {text(obj(t)["id"]): obj(t) for t in array(match["teams"])}
    summary_teams: list[JSON] = []
    vocabulary: list[str] = []
    for side in ("blue", "red"):
        meta = obj(metadata[f"{side}TeamMetadata"])
        team = teams[text(meta["esportsTeamId"])]
        players = [obj(p) for p in array(meta["participantMetadata"])]
        picks = [champions.get(text(p["championId"]), text(p["championId"])) for p in players]
        player_names = [text(p["summonerName"]).split(" ", 1)[-1] for p in players]
        name = text(team["name"])
        # The public feed exposes picks, but these endpoints do not expose bans.
        summary_teams.append({"name": name, "picks": picks, "bans": None})
        vocabulary.extend([name, *player_names, *picks])
    number = integer(game["number"])
    return (
        {"game": number, "teams": summary_teams},
        f"league:{text(event['id'])}:{number}",
        tuple(vocabulary),
    )
