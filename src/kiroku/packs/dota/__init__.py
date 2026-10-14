"""Title-relevant Dota Vocabulary; this slice has no live match data."""

import logging
import time
from pathlib import Path

import httpx

from kiroku.pack import LiveContext
from kiroku.packs.dota.data import (
    API,
    CONSTANTS,
    DotaData,
    contains_term,
    heroes,
    items,
    players,
    teams,
    title_teams,
)

VOCABULARY_CHARS = 350
log = logging.getLogger(__name__)


def vocabulary(title: str, rosters: tuple[str, ...], catalogue: tuple[str, ...]) -> str:
    candidates = (*rosters, *(name for name in catalogue if contains_term(name, title)))
    out = "Dota 2."
    for term in dict.fromkeys(candidates):
        if len(out) + len(term) + 2 <= VOCABULARY_CHARS:
            out += f" {term},"
    remaining = VOCABULARY_CHARS - len(out) - 1
    if remaining > 0:
        tail = title if len(title) <= remaining else title[: remaining + 1].rpartition(" ")[0]
        out += " " + tail
    return out.strip(" ,")


class DotaPack:
    name = "dota"
    categories = ("Dota 2",)

    def __init__(self, http: httpx.AsyncClient, data_dir: Path) -> None:
        self.data = DotaData(http, data_dir)

    async def vocabulary(self, title: str, live: LiveContext | None) -> str:
        roster: list[str] = []
        try:
            catalogue = await self.data.cached("teams", f"{API}/teams", teams)
            for team in title_teams(catalogue, title, time.time()):
                try:
                    members = await self.data.cached(
                        f"players-{team.id}", f"{API}/teams/{team.id}/players", players
                    )
                except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as e:
                    log.warning("Dota roster %d unavailable (%s)", team.id, e)
                    continue
                roster.extend(player.name for player in members)
        except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as e:
            log.warning("Dota teams unavailable (%s)", e)
        names: list[str] = []
        for name, parse in (("heroes", heroes), ("items", items)):
            try:
                names.extend(await self.data.cached(name, f"{CONSTANTS}/{name}.json", parse))
            except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as e:
                log.warning("Dota %s unavailable (%s)", name, e)
        return vocabulary(title, tuple(roster), tuple(names))

    async def get_live_match(self, title: str) -> LiveContext | None:
        return None
