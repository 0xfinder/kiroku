"""Jev (TypeSafe AI) behind the Gate interface.

One POST per Window: Mode as a `choice` question and each Rule as a `noul` (yes/no) question,
all against the same `state`. Question keys are never shown to the model. Jev bills input
tokens only ($0.042 per million), reported in `usage`. Retries happen here; the caller sees an
answer, OverBudget, GateFailed, or GateAuthError.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from decimal import ROUND_UP, Decimal
from typing import Any, Final, cast

import httpx

from kiroku.config import Secret
from kiroku.gate import (
    MODE_CRITERIA,
    MODE_INSTRUCTIONS,
    GateAnswer,
    GateAuthError,
    GateBadResponse,
    GateRequest,
    GateUnavailable,
    Mode,
)
from kiroku.gate.windows import JSON
from kiroku.ledger import CostEntry, Purpose, Tokens

log = logging.getLogger(__name__)

PROVIDER: Final = "jev"
URL: Final = "https://api.typesafe.ai/v1/systemone"
# Pinned: a recorded evaluation (spikes/gate_split.py) is valid for one model version.
DEFAULT_MODEL: Final = "jev-1.13.0"
USD_PER_MILLION_INPUT: Final = Decimal("0.042")
# Budget estimate: one token per BYTES_PER_TOKEN bytes of the request body. Real requests run
# at about 2.3 bytes per token, so this over-estimates; a test holds it to the recordings.
BYTES_PER_TOKEN: Final = 2
MODE_KEY: Final = "mode"
REQUEST_TIMEOUT_S: Final = 15.0


class JevGate:
    def __init__(
        self,
        http: httpx.AsyncClient,
        key: Secret,
        *,
        model: str = DEFAULT_MODEL,
        give_up_s: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._key = key
        self._model = model
        self._give_up_s = give_up_s
        self._sleep = sleep
        self._clock = clock
        # Clock time before which no session may call Jev. A 429 is per API key, so it pauses
        # every session. Writers only raise it.
        self._not_before = 0.0

    @property
    def name(self) -> str:
        return f"{PROVIDER}:{self._model}"

    async def evaluate(self, request: GateRequest) -> GateAnswer:
        body = request_body(self._model, request)
        estimate = estimate_tokens(body)
        await request.guard.check(usd(estimate))  # OverBudget propagates; nothing was sent
        first = self._clock()
        attempt = 0
        maybe_billed = 0  # sent, but the response was lost: Jev may have billed it
        last = ""
        while True:
            wait = self._not_before - self._clock()
            if attempt and last != "HTTP 429":
                wait = max(wait, _backoff(attempt))
            # Give up rather than wait past the deadline: a long Retry-After must not stall
            # every session's Gate. The shared pause still holds for later Windows.
            if (attempt or wait > 0) and self._clock() + max(wait, 0) - first > self._give_up_s:
                raise GateUnavailable(
                    f"jev unavailable after {attempt} attempts: {last or 'rate limited'}",
                    self._lost_costs(request.purpose, estimate, maybe_billed),
                )
            if wait > 0:
                await self._sleep(wait)
            attempt += 1
            started = self._clock()
            try:
                response = await self._http.post(
                    URL,
                    headers={"Authorization": f"Bearer {self._key.reveal()}"},
                    json=body,
                    timeout=REQUEST_TIMEOUT_S,
                )
            except httpx.RequestError as e:  # transport trouble, or a body httpx can't decode
                if not isinstance(e, httpx.ConnectError | httpx.ConnectTimeout):
                    maybe_billed += 1
                last = f"{type(e).__name__}: {e}"
                log.warning("Jev request failed (%s); retrying", last)
                continue
            latency_ms = int((self._clock() - started) * 1000)
            status = response.status_code
            if status in (401, 403):
                raise GateAuthError(f"Jev rejected the API key (HTTP {status})")
            if status == 429:
                wait = _retry_after(response.headers) or _backoff(attempt)
                self._not_before = max(self._not_before, self._clock() + wait)
                last = "HTTP 429"
                log.warning("Jev rate limit; pausing all sessions for %.1f s", wait)
                continue
            if status >= 500:  # 529 is Jev's "overloaded"
                last = f"HTTP {status}"
                log.warning("Jev HTTP %d; retrying", status)
                continue
            if status >= 400:
                # Our request is wrong. Retrying can't help, and nothing was billed.
                raise GateBadResponse(f"jev HTTP {status}: {response.text[:200]}")
            try:
                return parse_answer(response.json(), request, maybe_billed, latency_ms)
            except (ValueError, KeyError, TypeError) as e:
                raise GateBadResponse(
                    f"jev malformed response: {e}",
                    self._lost_costs(request.purpose, estimate, maybe_billed + 1),
                ) from e

    def _lost_costs(self, purpose: Purpose, estimate: int, calls: int) -> tuple[CostEntry, ...]:
        """What possibly billed calls cost, from the estimate: over-report, never under."""
        if not calls:
            return ()
        tokens = estimate * calls
        return (CostEntry(PROVIDER, self._model, purpose, Tokens(tokens), usd(tokens), 0),)


def rule_key(rule_id: int) -> str:
    return f"rule_{rule_id}"


def request_body(model: str, request: GateRequest) -> dict[str, JSON]:
    """The exact POST body. Pure, so the live evaluation and the replay test share it."""
    questions: dict[str, JSON] = {}
    if request.ask_mode:
        questions[MODE_KEY] = {
            "type": "choice",
            "instructions": MODE_INSTRUCTIONS,
            "criteria": {mode.value: text for mode, text in MODE_CRITERIA.items()},
        }
    for q in request.rules:
        questions[rule_key(q.rule_id)] = {"type": "noul", "instructions": q.question}
    return {"model": model, "state": request.state.as_json(), "questions": questions}


def estimate_tokens(body: Mapping[str, JSON]) -> int:
    return math.ceil(len(json.dumps(body).encode()) / BYTES_PER_TOKEN)


def usd(input_tokens: int) -> Decimal:
    """Rounded up to the ledger's precision, so the ledger never under-counts a call."""
    return (USD_PER_MILLION_INPUT * input_tokens / 1_000_000).quantize(
        Decimal("0.000001"), rounding=ROUND_UP
    )


def parse_answer(
    raw: object, request: GateRequest, maybe_billed: int, latency_ms: int
) -> GateAnswer:
    """A 200 body -> GateAnswer. Raises ValueError, KeyError or TypeError when any expected
    answer is missing or out of range: a partial answer is not stored as decisions."""
    if not isinstance(raw, dict):
        raise TypeError(f"body is {type(raw).__name__}, not an object")
    body = cast(dict[str, Any], raw)
    answers = cast(dict[str, Any], body["answers"])
    usage = cast(dict[str, Any], body["usage"])
    input_tokens, output_tokens = int(usage["input_tokens"]), int(usage.get("output_tokens", 0))
    probabilities = {
        q.rule_id: _probability(answers[rule_key(q.rule_id)]["noul"]) for q in request.rules
    }
    mode = mode_probability = None
    if request.ask_mode:
        answer = cast(dict[str, Any], answers[MODE_KEY])
        mode = Mode(answer["choice"])
        mode_probability = _probability(cast(dict[str, Any], answer["probabilities"])[mode.value])
    model = str(body.get("model") or "unknown")
    # Lost responses before this one may have been billed too: count them at this call's size.
    tokens = input_tokens * (1 + maybe_billed)
    cost = CostEntry(
        PROVIDER,
        model,
        request.purpose,
        Tokens(tokens, output_tokens * (1 + maybe_billed)),
        usd(tokens),
        latency_ms,
    )
    return GateAnswer(f"{PROVIDER}:{model}", probabilities, mode, mode_probability, (cost,))


def _probability(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"probability {value!r} is not a number")
    p = float(value)
    if not 0.0 <= p <= 1.0:  # also rejects NaN
        raise ValueError(f"probability {p} is outside [0, 1]")
    return p


def _backoff(attempt: int) -> float:
    return float(min(2 ** (attempt - 1), 8))


def _retry_after(headers: Mapping[str, str]) -> float | None:
    try:
        return max(float(headers["retry-after"]), 1.0)
    except (KeyError, ValueError):
        return None
