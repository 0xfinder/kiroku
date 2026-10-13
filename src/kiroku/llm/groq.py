"""Groq JSON completions. One shared adapter paces Extraction and Fallback calls."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any, Final, Literal

import httpx
import psycopg

from kiroku.budget import OverBudget
from kiroku.config import Secret
from kiroku.gate.windows import JSON
from kiroku.groq_http import go_seconds, retry_delay
from kiroku.ledger import CostEntry, Tokens
from kiroku.llm import (
    LLMAttempt,
    LLMAuthError,
    LLMBadResponse,
    LLMCancelled,
    LLMRequest,
    LLMResult,
    LLMUnavailable,
    object_body,
)

PROVIDER: Final = "groq"
MODEL: Final = "openai/gpt-oss-120b"
URL: Final = "https://api.groq.com/openai/v1/chat/completions"
BYTES_PER_TOKEN: Final = 3


@dataclass(frozen=True, slots=True)
class TokenPrice:
    input_per_million: Decimal
    output_per_million: Decimal

    def usd(self, usage: Tokens) -> Decimal:
        return (
            (self.input_per_million * usage.input + self.output_per_million * usage.output)
            / 1_000_000
        ).quantize(Decimal("0.000001"), rounding=ROUND_UP)


# List price, including hidden reasoning output, verified 2026-10-12:
# https://console.groq.com/docs/model/openai/gpt-oss-120b
PRICE: Final = TokenPrice(Decimal("0.15"), Decimal("0.60"))


def request_body(model: str, request: LLMRequest, reasoning_effort: str = "low") -> dict[str, JSON]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ],
        "response_format": {"type": "json_object"},
        "reasoning_effort": reasoning_effort,
        "include_reasoning": False,
        "temperature": 0,
        "max_completion_tokens": request.max_output_tokens,
    }


def estimate_tokens(body: Mapping[str, JSON], output: int) -> Tokens:
    return Tokens(math.ceil(len(json.dumps(body).encode()) / BYTES_PER_TOKEN), output)


def token_count(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError("invalid token count")
    return raw


@dataclass(slots=True)
class Dispatch:
    started: bool = False


class GroqLLM:
    def __init__(
        self,
        http: httpx.AsyncClient,
        key: Secret,
        *,
        model: str = MODEL,
        reasoning_effort: Literal["low", "medium", "high"] = "low",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http, self._key, self._model = http, key, model
        self._effort, self._sleep, self._clock = reasoning_effort, sleep, clock
        self._not_before = 0.0
        self._remaining_requests: int | None = None
        self._requests_reset_at = 0.0
        self._admission = asyncio.Lock()
        self._inflight = 0

    @property
    def name(self) -> str:
        return f"{PROVIDER}:{self._model}"

    async def complete_json[T](
        self, request: LLMRequest, parse: Callable[[Mapping[str, Any]], T]
    ) -> LLMResult[T]:
        body = request_body(self._model, request, self._effort)
        estimate = estimate_tokens(body, request.max_output_tokens)
        first = self._clock()
        costs: list[CostEntry] = []
        attempts: list[LLMAttempt] = []
        attempt, bad = 0, 0
        wait, last = 0.0, "rate limited"
        while True:
            wait = max(wait, self._not_before - self._clock(), 0.0)
            left = request.give_up_s - (self._clock() - first)
            if left <= 0 or wait >= left:
                raise LLMUnavailable(
                    f"groq unavailable after {attempt} attempts: {last}",
                    tuple(costs),
                    tuple(attempts),
                )
            try:
                if wait:
                    await self._sleep(wait)
            except asyncio.CancelledError:
                raise LLMCancelled(tuple(costs), tuple(attempts)) from None
            try:
                await request.guard.check(PRICE.usd(estimate) + sum(c.usd for c in costs))
            except asyncio.CancelledError:
                raise LLMCancelled(tuple(costs), tuple(attempts)) from None
            except psycopg.OperationalError:
                raise LLMUnavailable(
                    "budget database unavailable", tuple(costs), tuple(attempts)
                ) from None
            except OverBudget as e:
                raise OverBudget(e.refusal, tuple(costs), tuple(attempts)) from e
            attempt += 1
            started = self._clock()
            estimated = CostEntry(
                PROVIDER, self._model, request.purpose, estimate, PRICE.usd(estimate), 0
            )
            dispatch = Dispatch()
            try:
                response = await self._send(request, body, first, dispatch)
                if response is None:
                    continue
            except asyncio.CancelledError:
                if dispatch.started:
                    costs.append(estimated)
                    attempts.append(LLMAttempt("", "cancelled request; provider outcome unknown"))
                raise LLMCancelled(tuple(costs), tuple(attempts)) from None
            except LLMUnavailable as e:
                raise LLMUnavailable(e.reason, tuple(costs), tuple(attempts)) from e
            except httpx.RequestError as e:
                if not isinstance(e, httpx.ConnectError | httpx.ConnectTimeout):
                    costs.append(estimated)
                last = type(e).__name__
                wait = retry_delay(None, {}, attempt)
                continue
            latency = int((self._clock() - started) * 1000)
            status = response.status_code
            if status in (401, 403):
                raise LLMAuthError(
                    f"Groq rejected the API key (HTTP {status})", tuple(costs), tuple(attempts)
                )
            if status == 429 or status >= 500:
                attempts.append(LLMAttempt(response.text, f"HTTP {status}"))
                wait = retry_delay(status, response.headers, attempt)
                if status == 429:
                    self._not_before = max(self._not_before, self._clock() + wait)
                last = f"HTTP {status}"
                continue
            if status >= 400:
                try:
                    invalid = object_body(response.json()).get("error", {}).get("code") == (
                        "json_validate_failed"
                    )
                except (ValueError, TypeError, AttributeError):
                    invalid = False
                if not invalid:
                    attempts.append(LLMAttempt(response.text, f"HTTP {status}"))
                    raise LLMBadResponse(f"groq HTTP {status}", tuple(costs), tuple(attempts))
                attempts.append(LLMAttempt(response.text, "provider rejected JSON"))
                costs.append(estimated)
                bad += 1
                if bad >= 2:
                    raise LLMBadResponse("groq invalid JSON twice", tuple(costs), tuple(attempts))
                wait = 0.0
                continue
            counted = False
            content = response.text
            try:
                raw = object_body(response.json())
                usage = object_body(raw["usage"])
                tokens = Tokens(
                    token_count(usage["prompt_tokens"]), token_count(usage["completion_tokens"])
                )
                model = raw["model"]
                if not isinstance(model, str) or not model:
                    raise ValueError("missing model")
                costs.append(
                    CostEntry(PROVIDER, model, request.purpose, tokens, PRICE.usd(tokens), latency)
                )
                counted = True
                choice = object_body(raw["choices"][0])
                if choice["finish_reason"] != "stop":
                    raise ValueError("completion truncated or unfinished")
                content = object_body(choice["message"])["content"]
                if not isinstance(content, str):
                    raise ValueError("missing completion text")
                value = parse(object_body(json.loads(content)))
            except (ValueError, KeyError, TypeError, IndexError) as e:
                attempts.append(LLMAttempt(content, str(e)))
                if not counted:
                    costs.append(estimated)
                bad += 1
                if bad >= 2:
                    raise LLMBadResponse(
                        "groq malformed completion twice", tuple(costs), tuple(attempts)
                    ) from None
                wait = 0.0
                continue
            attempts.append(LLMAttempt(content))
            return LLMResult(value, f"{PROVIDER}:{model}", tuple(costs), tuple(attempts))

    async def _send(
        self, request: LLMRequest, body: Mapping[str, JSON], first: float, dispatch: Dispatch
    ) -> httpx.Response | None:
        # Only admission mutates the known available quota. HTTP calls run independently
        # so a slow Extraction cannot consume the Fallback's deadline while holding a lock.
        async with self._admission:
            if self._clock() >= self._requests_reset_at:
                self._remaining_requests = None
            if (
                self._remaining_requests is not None
                and self._remaining_requests <= request.min_remaining_requests
                and request.min_remaining_requests > 0
            ):
                raise LLMUnavailable("daily request quota reserved for Extraction")
            left = request.give_up_s - (self._clock() - first)
            if left <= 0:
                raise LLMUnavailable("groq unavailable: admission deadline reached")
            if self._not_before > self._clock():
                return None
            reserved = self._remaining_requests is not None
            if self._remaining_requests is not None:
                self._remaining_requests -= 1
            self._inflight += 1
        definite_failure = False
        try:
            dispatch.started = True
            response = await self._http.post(
                URL,
                headers={"Authorization": f"Bearer {self._key.reveal()}"},
                json=body,
                timeout=min(30.0, left),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            definite_failure = True
            raise
        finally:
            async with self._admission:
                self._inflight -= 1
                if definite_failure and reserved and self._remaining_requests is not None:
                    self._remaining_requests += 1
        async with self._admission:
            with contextlib.suppress(KeyError, ValueError):
                remaining = max(
                    0, int(response.headers["x-ratelimit-remaining-requests"]) - self._inflight
                )
                self._remaining_requests = (
                    remaining
                    if self._remaining_requests is None
                    else min(remaining, self._remaining_requests)
                )
                reset = go_seconds(response.headers.get("x-ratelimit-reset-requests", "24h"))
                self._requests_reset_at = self._clock() + (reset if reset is not None else 86_400)
            if response.status_code == 429:
                self._not_before = max(
                    self._not_before, self._clock() + retry_delay(429, response.headers, 1)
                )
        return response
