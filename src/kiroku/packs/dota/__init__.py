"""Title-relevant Dota Vocabulary; this slice has no live match data."""

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from kiroku.pack import LiveContext
from kiroku.packs.common import CACHE_ERRORS, budgeted_vocabulary, contains_term
from kiroku.packs.common import VOCABULARY_CHARS as VOCABULARY_CHARS
from kiroku.packs.dota.data import (
    API,
    CONSTANTS,
    DotaData,
    heroes,
    items,
    players,
    teams,
    title_teams,
)

log = logging.getLogger(__name__)


def vocabulary(title: str, rosters: tuple[str, ...], catalogue: tuple[str, ...]) -> str:
    candidates = (*rosters, *(name for name in catalogue if contains_term(name, title)))
    return budgeted_vocabulary("Dota 2.", candidates, title)


class DotaPack:
    name = "dota"
    categories = ("Dota 2",)

    def __init__(self, http: httpx.AsyncClient, data_dir: Path) -> None:
        self.data = DotaData(http, data_dir)

    async def _names(
        self, name: str, parse: Callable[[object], tuple[str, ...]]
    ) -> tuple[str, ...]:
        try:
            return await self.data.cached(name, f"{CONSTANTS}/{name}.json", parse)
        except CACHE_ERRORS as e:
            log.warning("Dota %s unavailable (%s)", name, e)
            return ()

    async def _roster(self, team_id: int) -> tuple[str, ...]:
        try:
            members = await self.data.cached(
                f"players-{team_id}", f"{API}/teams/{team_id}/players", players
            )
            return tuple(player.name for player in members)
        except CACHE_ERRORS as e:
            log.warning("Dota roster %d unavailable (%s)", team_id, e)
            return ()

    async def _rosters(self, title: str) -> tuple[str, ...]:
        try:
            catalogue = await self.data.cached("teams", f"{API}/teams", teams)
        except CACHE_ERRORS as e:
            log.warning("Dota teams unavailable (%s)", e)
            return ()
        rosters = await asyncio.gather(
            *(self._roster(team.id) for team in title_teams(catalogue, title, time.time()))
        )
        return tuple(name for roster in rosters for name in roster)

    async def vocabulary(self, title: str, live: LiveContext | None) -> str:
        roster, hero_names, item_names = await asyncio.gather(
            self._rosters(title), self._names("heroes", heroes), self._names("items", items)
        )
        return vocabulary(title, roster, (*hero_names, *item_names))

    async def get_live_match(self, title: str) -> LiveContext | None:
        return None
