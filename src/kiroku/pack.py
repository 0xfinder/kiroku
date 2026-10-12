"""The Pack interface extracted from the League live transcript path (D12)."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from kiroku.gate.windows import JSON


@dataclass(frozen=True, slots=True)
class LiveContext:
    summary: Mapping[str, JSON]
    game_key: str
    vocabulary: tuple[str, ...] = ()


class Pack(Protocol):
    @property
    def name(self) -> str: ...

    async def vocabulary(self, title: str, live: LiveContext | None) -> str: ...

    async def get_live_match(self, title: str) -> LiveContext | None: ...


type PackSelector = Callable[[str | None], Pack | None]
