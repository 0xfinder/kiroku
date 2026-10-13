"""D20: an outage bypasses the primary for five minutes, without slowing later Windows."""

import logging
import time
from collections.abc import Callable
from dataclasses import replace

import psycopg

from kiroku.budget import OverBudget
from kiroku.gate import (
    Gate,
    GateAnswer,
    GateAuthError,
    GateBadResponse,
    GateFailed,
    GateRequest,
    GateUnavailable,
)
from kiroku.ledger import CostEntry
from kiroku.llm import LLMDatabaseError

log = logging.getLogger(__name__)


class FallbackGate:
    def __init__(
        self,
        primary: Gate,
        fallback: Gate,
        *,
        bypass_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary, self._fallback = primary, fallback
        self._bypass_s, self._clock = bypass_s, clock
        self._until = 0.0
        self._why = ""
        self._fallback_disabled = False

    @property
    def name(self) -> str:
        return self._primary.name

    async def evaluate(self, request: GateRequest) -> GateAnswer:
        costs: tuple[CostEntry, ...] = ()
        why = self._why
        if self._fallback_disabled or self._clock() >= self._until:
            try:
                answer = await self._primary.evaluate(request)
            except (GateUnavailable, GateBadResponse) as e:
                why, costs = e.reason, e.costs
                if isinstance(e, GateUnavailable):
                    self._until, self._why = self._clock() + self._bypass_s, why
                if not self._fallback_disabled:
                    log.warning("Gate: %s; falling back to %s", why, self._fallback.name)
            else:
                if self._why:
                    log.info("%s answered again", self._primary.name)
                    self._why = ""
                return answer
        if self._fallback_disabled:
            raise GateFailed(
                f"{why}; fallback disabled after authentication failure", costs, reported=True
            )
        try:
            answer = await self._fallback.evaluate(request)
        except LLMDatabaseError as e:
            raise LLMDatabaseError(costs + e.costs, e.attempts) from e
        except psycopg.OperationalError:
            if costs:
                raise LLMDatabaseError(costs, ()) from None
            raise
        except OverBudget as e:
            raise OverBudget(e.refusal, costs + e.costs) from e
        except GateAuthError as e:
            self._fallback_disabled = True
            log.error("%s: %s; fallback is off for this process", self._fallback.name, e)
            raise GateFailed(
                f"{why}; fallback authentication failed: {e}", costs + e.costs, reported=True
            ) from e
        except GateFailed as e:
            raise GateFailed(f"{why}; fallback: {e.reason}", costs + e.costs) from e
        return replace(answer, note=f"fallback: {why}", costs=costs + answer.costs)
