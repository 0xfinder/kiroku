"""Provider-neutral JSON completions for Extraction and the Gate Fallback."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import psycopg

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
class LLMAttempt:
    output: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LLMResult[T]:
    value: T
    model: str
    costs: tuple[CostEntry, ...]
    attempts: tuple[LLMAttempt, ...] = ()


class LLMDatabaseError(psycopg.OperationalError):
    """A retryable budget database failure after paid attempts; never a provider failure."""

    def __init__(self, costs: tuple[CostEntry, ...], attempts: tuple[LLMAttempt, ...]) -> None:
        super().__init__("budget database unavailable after completion attempts")
        self.costs, self.attempts = costs, attempts


class LLMFailed(Exception):
    def __init__(
        self,
        reason: str,
        costs: tuple[CostEntry, ...] = (),
        attempts: tuple[LLMAttempt, ...] = (),
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.costs = costs
        self.attempts = attempts


class LLMUnavailable(LLMFailed):
    def __init__(
        self,
        reason: str,
        costs: tuple[CostEntry, ...] = (),
        attempts: tuple[LLMAttempt, ...] = (),
        *,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(reason, costs, attempts)
        self.retry_after_s = retry_after_s


class LLMBadResponse(LLMFailed):
    pass


class LLMAuthError(KirokuError):
    def __init__(
        self,
        reason: str,
        costs: tuple[CostEntry, ...] = (),
        attempts: tuple[LLMAttempt, ...] = (),
    ) -> None:
        super().__init__(reason)
        self.costs = costs
        self.attempts = attempts


class LLMCancelled(asyncio.CancelledError):
    def __init__(self, costs: tuple[CostEntry, ...], attempts: tuple[LLMAttempt, ...]) -> None:
        super().__init__("completion cancelled")
        self.costs, self.attempts = costs, attempts


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
