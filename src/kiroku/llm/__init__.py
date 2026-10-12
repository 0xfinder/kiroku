"""Provider-neutral JSON completions for Extraction and the Gate Fallback."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from kiroku.budget import BudgetGuard
from kiroku.errors import KirokuError
from kiroku.ledger import CostEntry, Purpose


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system: str
    user: str
    purpose: Purpose
    guard: BudgetGuard
    max_output_tokens: int
    give_up_s: float
    min_remaining_requests: int = 0


@dataclass(frozen=True, slots=True)
class LLMResult[T]:
    value: T
    model: str
    costs: tuple[CostEntry, ...]


class LLMFailed(Exception):
    def __init__(self, reason: str, costs: tuple[CostEntry, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.costs = costs


class LLMUnavailable(LLMFailed):
    pass


class LLMBadResponse(LLMFailed):
    pass


class LLMAuthError(KirokuError):
    def __init__(self, reason: str, costs: tuple[CostEntry, ...] = ()) -> None:
        super().__init__(reason)
        self.costs = costs


class LLM(Protocol):
    @property
    def name(self) -> str: ...

    async def complete_json[T](
        self, request: LLMRequest, parse: Callable[[Mapping[str, Any]], T]
    ) -> LLMResult[T]: ...


def object_body(raw: object) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("expected a JSON object")
    return cast(dict[str, Any], raw)
