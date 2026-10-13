"""League Vocabulary and live match context from Data Dragon and lolesports."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from kiroku.pack import LiveContext
from kiroku.packs.league.data import (
    DDRAGON,
    FEED,
    PERSISTED,
    LeagueData,
    array,
    contains_term,
    match_context,
    names,
    obj,
    roster_terms,
    select_event,
    text,
    versions,
)
from kiroku.packs.league.notes import notes_schema

VOCABULARY_CHARS = 350


def vocabulary(
    title: str,
    champions: dict[str, str],
    items: dict[str, str],
    rosters: tuple[str, ...],
    live: LiveContext | None,
) -> str:
    """Match terms first; otherwise rosters and names mentioned in the title.

    The catalogue is too large for Whisper's 224-token prompt. Only terms relevant to
    this stream are sent, with room left for the previous transcript's tail.
    """
    candidates = list(live.vocabulary if live else rosters)
    candidates.extend(n for n in (*champions.values(), *items.values()) if contains_term(n, title))
    out = "League of Legends."
    for term in dict.fromkeys(candidates):
        if len(out) + len(term) + 2 <= VOCABULARY_CHARS:
            out += f" {term},"
    remaining = VOCABULARY_CHARS - len(out) - 1
    if remaining > 0 and live is None:
        tail = title if len(title) <= remaining else title[: remaining + 1].rpartition(" ")[0]
        out += " " + tail
    return out.strip(" ,")


class LeaguePack:
    name = "league"
    categories = ("League of Legends",)
    notes_schema = staticmethod(notes_schema)

    def __init__(self, http: httpx.AsyncClient, data_dir: Path, key: str) -> None:
        self.data = LeagueData(http, data_dir, key)

    async def _catalogue(self) -> tuple[dict[str, str], dict[str, str]]:
        available = await self.data.cached(
            "versions", f"{DDRAGON}/api/versions.json", versions, memoize=True
        )
        version = available[0]
        base = f"{DDRAGON}/cdn/{version}/data/en_US"
        champions = await self.data.cached(
            f"champion-{version}", f"{base}/champion.json", names, memoize=True
        )
        items = await self.data.cached(f"item-{version}", f"{base}/item.json", names, memoize=True)
        return champions, items

    async def vocabulary(self, title: str, live: LiveContext | None) -> str:
        champions, items = await self._catalogue()
        rosters = await self.data.cached(
            "teams",
            f"{PERSISTED}/getTeams?hl=en-US",
            lambda data: roster_terms(data, title),
            website=True,
        )
        return vocabulary(title, champions, items, rosters, live)

    async def get_live_match(self, title: str) -> LiveContext | None:
        data = await self.data.persisted("getLive")
        event = select_event({"data": data}, title)
        if event is None:
            return None
        event = obj(
            (await self.data.persisted("getEventDetails", id=text(obj(event["match"])["id"])))[
                "event"
            ]
        )
        games = [
            obj(g)
            for g in array(obj(event["match"])["games"])
            if obj(g).get("state") == "inProgress"
        ]
        if len(games) != 1:
            return None  # between games, not a stale previous game's picks
        # Same delayed, ten-second aligned feed clock as live-lol-esports.
        at = datetime.now(UTC) - timedelta(seconds=60)
        at = at.replace(second=at.second // 10 * 10, microsecond=0)
        window = await self.data.get(
            f"{FEED}/window/{text(games[0]['id'])}", params={"startingTime": at.isoformat()}
        )
        champions, _ = await self._catalogue()
        summary, key, terms = match_context(event, window, champions)
        return LiveContext(summary, key, terms)
