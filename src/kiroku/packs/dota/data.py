"""Free Dota catalogues and conservative title-team selection."""

import re
from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from kiroku.packs.common import DATA_TTL_S as DATA_TTL_S
from kiroku.packs.common import DataError, PackData, array, integer, obj, text

CONSTANTS = "https://raw.githubusercontent.com/odota/dotaconstants/master/build"
API = "https://api.opendota.com/api"
TEAM_RECENCY_S = 180 * 24 * 3600
RETRY_S = 5 * 60
# These common pub-title words collide with real team names/tags in /teams.
TITLE_WORDS = frozenset({"mmr", "alt", "re", "grind", "pub", "pubs", "ranked", "games", "game"})


def heroes(data: object) -> tuple[str, ...]:
    names = tuple(text(obj(v)["localized_name"], strip=True) for v in obj(data).values())
    if not names:
        raise DataError("expected a nonempty hero catalogue")
    return names


def items(data: object) -> tuple[str, ...]:
    # Internal items can have missing, empty or whitespace-only display names.
    names: list[str] = []
    for value in obj(data).values():
        try:
            names.append(text(obj(value).get("dname"), strip=True))
        except DataError:
            continue
    if not names:
        raise DataError("expected named items")
    return tuple(names)


@dataclass(frozen=True, slots=True)
class Team:
    id: int
    name: str
    tag: str
    last_match_time: int


def teams(data: object) -> tuple[Team, ...]:
    result: list[Team] = []
    for value in array(data):
        try:
            row = obj(value)
            tag = row["tag"]
            if not isinstance(tag, str):
                continue
            team = Team(
                integer(row["team_id"]),
                text(row["name"], strip=True),
                tag.strip(),
                integer(row["last_match_time"]),
            )
            if team.id > 0:
                result.append(team)
        except (DataError, KeyError):
            continue
    if not result:
        raise DataError("expected valid named teams")
    return tuple(result)


def short_name(name: str) -> str:
    return re.sub(r"^team[ .]+", "", name, flags=re.I).strip()


def tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", value))


@dataclass(frozen=True)
class TeamIndex:
    names: dict[tuple[str, ...], tuple[Team, ...]]
    tags: dict[tuple[str, ...], tuple[Team, ...]]
    full_names: dict[tuple[str, ...], tuple[Team, ...]]
    width: int


@lru_cache(maxsize=4)
def team_index(catalogue: tuple[Team, ...]) -> TeamIndex:
    names: dict[tuple[str, ...], list[Team]] = {}
    tags: dict[tuple[str, ...], list[Team]] = {}
    full_names: dict[tuple[str, ...], list[Team]] = {}
    for team in sorted(catalogue, key=lambda team: team.last_match_time, reverse=True):
        for term in dict.fromkeys((team.name, short_name(team.name))):
            key = tokens(term.casefold())
            if key:
                names.setdefault(key, []).append(team)
        if len(team.tag) >= 2:
            tags.setdefault(tokens(team.tag), []).append(team)
        key = tokens(team.name.casefold())
        if len(key) >= 2:
            full_names.setdefault(key, []).append(team)
    return TeamIndex(
        {key: tuple(value) for key, value in names.items()},
        {key: tuple(value) for key, value in tags.items()},
        {key: tuple(value) for key, value in full_names.items()},
        max((len(key) for key in (*names, *tags)), default=0),
    )


def title_teams(catalogue: tuple[Team, ...], title: str, now: float) -> tuple[Team, ...]:
    index = team_index(catalogue)
    spans = tuple(re.finditer(r"\w+", title))
    words = tuple(match[0] for match in spans)
    starts = tuple(match.start() for match in spans)
    folded = tuple(word.casefold() for word in words)

    def active(candidates: tuple[Team, ...]) -> Team | None:
        return next(
            (team for team in candidates if 0 <= now - team.last_match_time <= TEAM_RECENCY_S),
            None,
        )

    def boundary(side: tuple[str, ...], *, left: bool) -> Team | None:
        for size in range(min(len(side), index.width), 0, -1):
            key = side[-size:] if left else side[:size]
            if len(key) == 1 and key[0].casefold() in TITLE_WORDS:
                continue
            # Tags are exact-case and only used beside a matchup separator.
            candidates = (
                *index.names.get(tuple(w.casefold() for w in key), ()),
                *index.tags.get(key, ()),
            )
            # A single-word name identical to its tag also obeys tag casing.
            candidates = tuple(
                team
                for team in candidates
                if not (len(key) == 1 and team.name == team.tag and key[0] != team.tag)
            )
            chosen = active(
                tuple(sorted(candidates, key=lambda t: t.last_match_time, reverse=True))
            )
            if chosen is not None:
                return chosen
        return None

    def distinct(candidates: list[Team]) -> tuple[Team, ...]:
        result: list[Team] = []
        seen_names: set[str] = set()
        seen_tags: set[str] = set()
        for team in candidates:
            name, tag = short_name(team.name).casefold(), team.tag.casefold()
            if any(
                other.last_match_time > team.last_match_time
                and (
                    short_name(other.name).casefold() == name
                    or (tag and other.tag.casefold() == tag)
                )
                for other in candidates
            ):
                continue
            if name in seen_names or (tag and tag in seen_tags):
                continue
            result.append(team)
            seen_names.add(name)
            if tag:
                seen_tags.add(tag)
            if len(result) == 2:
                break
        return tuple(result)

    # Tokenize the title once, then look up only terms touching each separator.
    separators = list(re.finditer(r"\s+(vs\.?|x|-)\s+", title, re.I))
    partial: list[Team] = []
    explicit = False
    for match in separators:
        offset = bisect_left(starts, match.start())
        separator_words = 0 if match[1] == "-" else 1
        left = boundary(words[:offset], left=True)
        right = boundary(words[offset + separator_words :], left=False)
        if left is not None and right is not None:
            return distinct([left, right])
        if match[1].casefold() != "-":
            explicit = True
            partial.extend(team for team in (left, right) if team is not None)
    if explicit:
        return distinct(partial)

    # Without a matchup, require a full multiword name, never a short name/tag.
    selected: list[Team] = []
    for start in range(len(words)):
        for size in range(min(index.width, len(words) - start), 1, -1):
            if (team := active(index.full_names.get(folded[start : start + size], ()))) is not None:
                selected.append(team)
                break
    return distinct(selected)


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


class DotaData(PackData):
    def __init__(self, http: httpx.AsyncClient, data_dir: Path) -> None:
        super().__init__(http, data_dir / "packs" / "dota", retry_s=RETRY_S, memoize=True)
