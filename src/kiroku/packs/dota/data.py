"""Free OpenDota data, validated before caching; stale data remains usable."""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

CONSTANTS = "https://raw.githubusercontent.com/odota/dotaconstants/master/build"
API = "https://api.opendota.com/api"
DATA_TTL_S = 24 * 3600
TEAM_RECENCY_S = 180 * 24 * 3600
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


def integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DataError("expected an integer")
    return value


def text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataError("expected a nonempty string")
    return value.strip()


def contains_term(term: str, title: str) -> bool:
    return bool(term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", title, re.I))


def heroes(data: object) -> tuple[str, ...]:
    names = tuple(text(obj(v)["localized_name"]) for v in obj(data).values())
    if not names:
        raise DataError("expected a nonempty hero catalogue")
    return names


def items(data: object) -> tuple[str, ...]:
    # dotaconstants includes internal items with no display name.
    names = tuple(
        text(n) for v in obj(data).values() if (n := obj(v).get("dname")) not in (None, "")
    )

    if not names:
        raise DataError("expected named items")
    return names


@dataclass(frozen=True, slots=True)
class Team:
    id: int
    name: str
    tag: str
    last_match_time: int


def teams(data: object) -> tuple[Team, ...]:
    result: list[Team] = []
    for value in array(data):
        row = obj(value)
        tag = row["tag"]
        if not isinstance(tag, str):
            raise DataError("expected a team tag string")
        result.append(
            Team(integer(row["team_id"]), text(row["name"]), tag, integer(row["last_match_time"]))
        )
    return tuple(result)


def short_name(name: str) -> str:
    return re.sub(r"^team[ .]+", "", name, flags=re.I).strip()


def title_teams(catalogue: tuple[Team, ...], title: str, now: float) -> tuple[Team, ...]:
    chosen: dict[str, tuple[int, Team]] = {}
    for team in catalogue:
        if not 0 <= now - team.last_match_time <= TEAM_RECENCY_S:
            continue
        terms = (team.name, short_name(team.name), team.tag)
        positions = [
            m.start()
            for term in terms
            if term
            for m in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", title, re.I)
        ]
        if not positions:
            continue
        identity = short_name(team.name).casefold()
        previous = chosen.get(identity)
        if previous is None or team.last_match_time > previous[1].last_match_time:
            chosen[identity] = (min(positions), team)
    return tuple(team for _, team in sorted(chosen.values(), key=lambda entry: entry[0]))


@dataclass(frozen=True, slots=True)
class Player:
    account_id: int
    name: str


def players(data: object) -> tuple[Player, ...]:
    result: list[Player] = []
    for value in array(data):
        row = obj(value)
        account = integer(row["account_id"])
        member, name = row["is_current_team_member"], row["name"]
        if member is not None and not isinstance(member, bool):
            raise DataError("expected boolean or null membership")
        if name is not None and not isinstance(name, str):
            raise DataError("expected string or null player name")
        if member is True and isinstance(name, str) and name.strip():
            result.append(Player(account, name.strip()))
    return tuple(result)


class DotaData:
    def __init__(self, http: httpx.AsyncClient, data_dir: Path) -> None:
        self.http = http
        self.cache_dir = data_dir / "packs" / "dota"
        self._lock = asyncio.Lock()
        self._parsed: dict[tuple[str, object], tuple[int, object]] = {}

    async def cached[T](self, name: str, url: str, parse: Callable[[object], T]) -> T:
        async with self._lock:
            path = self.cache_dir / f"{name}.json"
            stale: tuple[T] | None = None
            memo_key = (name, parse)
            try:
                stat = path.stat()
                memo = self._parsed.get(memo_key)
                if memo is not None and memo[0] == stat.st_mtime_ns:
                    parsed = cast(T, memo[1])
                else:
                    parsed = parse(json.loads(path.read_text()))
                    self._parsed[memo_key] = (stat.st_mtime_ns, parsed)
                stale = (parsed,)
                if time.time() - stat.st_mtime < DATA_TTL_S:
                    return parsed
            except (OSError, ValueError, KeyError, IndexError):
                pass
            try:
                response = await self.http.get(url, timeout=10)
                response.raise_for_status()
                body: object = response.json()
                parsed = parse(body)
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(body))
                temporary.replace(path)
                self._parsed[memo_key] = (path.stat().st_mtime_ns, parsed)
            except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as e:
                if stale is None:
                    raise
                log.warning("Dota %s refresh failed; using stale cache (%s)", name, e)
                return stale[0]
            return parsed
