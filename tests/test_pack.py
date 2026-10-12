"""Category selection uses the injected Packs' declarations."""

from dataclasses import dataclass

from kiroku.pack import LiveContext, pack_selector


@dataclass
class StubPack:
    name: str
    categories: tuple[str, ...]

    async def vocabulary(self, title: str, live: LiveContext | None) -> str:
        return title

    async def get_live_match(self, title: str) -> LiveContext | None:
        return None


def test_selector_supports_multiple_packs_and_categories() -> None:
    league = StubPack("league", ("League of Legends",))
    dota = StubPack("dota", ("Dota 2", "Dota Underlords"))
    select = pack_selector([league, dota])
    assert select("League of Legends") is league
    assert select("Dota 2") is dota
    assert select("Dota Underlords") is dota
    assert select("Just Chatting") is None
    assert select(None) is None
