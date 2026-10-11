"""Budget caps (D8): hard per-session and per-month limits on paid calls.

Every paid call asks a BudgetGuard first, inside its provider adapter, just before the HTTP
request. The guard reads the Cost ledger each time: no cached totals and no reservations.
Check-then-act can overshoot a cap by the calls already in flight (a fraction of a cent at
today's prices); revisit if an adapter makes expensive concurrent calls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol

from kiroku import ledger
from kiroku.db import Pool

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Caps:
    session_usd: Decimal = Decimal("1.50")
    month_usd: Decimal = Decimal("20")  # D8


@dataclass(frozen=True, slots=True)
class Refusal:
    cap: Literal["session", "month"]
    limit: Decimal
    spent: Decimal
    estimate: Decimal

    def __str__(self) -> str:
        return (
            f"{self.cap} Budget cap ${self.limit} reached: "
            f"spent ${self.spent}, next call ~${self.estimate}"
        )


class OverBudget(Exception):
    """A paid call was refused. Expected and handled per call, so not a KirokuError."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


def refusal(
    caps: Caps, *, month_spent: Decimal, session_spent: Decimal | None, estimate: Decimal
) -> Refusal | None:
    """Refuse when spent + estimate would exceed a cap. The month is checked first.
    `session_spent=None` means the call belongs to no session (a Rule test)."""
    if month_spent + estimate > caps.month_usd:
        return Refusal("month", caps.month_usd, month_spent, estimate)
    if session_spent is not None and session_spent + estimate > caps.session_usd:
        return Refusal("session", caps.session_usd, session_spent, estimate)
    return None


def month_start(now: datetime) -> datetime:
    """The first instant of `now`'s calendar month in UTC."""
    now = now.astimezone(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class BudgetGuard(Protocol):
    async def check(self, estimate_usd: Decimal) -> None:
        """Return to allow the call; raise OverBudget to refuse it. A database error
        propagates: when spend can't be read, the call is not made (fail closed)."""
        ...


class Budget:
    """One per process, shared by every session."""

    def __init__(
        self, pool: Pool, caps: Caps, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._pool = pool
        self._caps = caps
        self._now = now
        # Caps currently refusing, per session (None: no session). A capped six-hour session
        # would otherwise log a warning per chunk.
        self._refusing: dict[int | None, Literal["session", "month"]] = {}

    def guard(self, session_id: int | None) -> BudgetGuard:
        return _Guard(self, session_id)

    async def check(self, session_id: int | None, estimate: Decimal) -> None:
        """BudgetGuard.check for `session_id`. Prefer passing `guard(session_id)` around."""
        async with self._pool.connection() as conn:
            month = await ledger.spent(conn, since=month_start(self._now()))
            session = (
                await ledger.spent(conn, session_id=session_id) if session_id is not None else None
            )
        refused = refusal(self._caps, month_spent=month, session_spent=session, estimate=estimate)
        where = f"session {session_id}" if session_id is not None else "no session"
        if refused is None:
            if self._refusing.pop(session_id, None) is not None:
                log.info("%s: paid calls allowed again", where)
            return
        if self._refusing.get(session_id) != refused.cap:
            self._refusing[session_id] = refused.cap
            log.warning("%s: %s; paid calls stop", where, refused)
        else:
            log.debug("%s: %s", where, refused)
        raise OverBudget(refused)


@dataclass(frozen=True, slots=True)
class _Guard:
    budget: Budget
    session_id: int | None

    async def check(self, estimate_usd: Decimal) -> None:
        await self.budget.check(self.session_id, estimate_usd)


class Unlimited:
    """A guard that allows everything, for tests and tools that don't spend."""

    async def check(self, estimate_usd: Decimal) -> None:
        return None
