"""Rule tests (#4): ask the Gate one Rule about stored Windows, without storing decisions.

Spike finding 3: a vaguely worded Rule passed almost every Window. Wording needs a fast loop,
so `kiroku rule test` replays a Rule (or a candidate rewording) against the exact state the
live Gate saw, and shows how its scores spread. The calls are real and paid: each writes a
Cost ledger row (purpose rule_test, no session) and counts against the monthly Budget cap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from kiroku import ledger
from kiroku.budget import BudgetGuard, OverBudget
from kiroku.db import Pool
from kiroku.gate import Gate, GateAuthError, GateFailed, GateRequest, RuleQuestion, passes
from kiroku.gate.windows import GateState
from kiroku.ledger import Purpose

CONCURRENCY = 4


@dataclass(frozen=True, slots=True)
class StoredWindow:
    id: int
    session_id: int
    start_ms: int
    state: GateState


@dataclass(frozen=True, slots=True)
class Trial:
    window: StoredWindow
    probability: float | None  # None when the call was refused or failed
    error: str | None = None


async def stored_windows(
    pool: Pool, *, session_id: int | None, last: int | None
) -> list[StoredWindow]:
    """Windows with speech in `new`, oldest first: one session's, or the latest `last`."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT * FROM (
                SELECT id, session_id, start_ms, state FROM gate_window
                WHERE state->>'new' <> '' AND (%(s)s::bigint IS NULL OR session_id = %(s)s)
                ORDER BY closed_at DESC, id DESC
                LIMIT %(n)s) w
            ORDER BY session_id, start_ms
            """,
            {"s": session_id, "n": last},
        )
        rows = await cur.fetchall()
    out: list[StoredWindow] = []
    for window_id, session, start_ms, raw in rows:
        state = cast(dict[str, Any], raw)
        out.append(
            StoredWindow(window_id, session, start_ms, GateState(state["context"], state["new"]))
        )
    return out


async def run(
    pool: Pool,
    gate: Gate,
    guard: BudgetGuard,
    question: RuleQuestion,
    windows: Sequence[StoredWindow],
) -> list[Trial]:
    """One paid call per Window, a few at a time. Decisions are not stored; costs are."""
    slots = asyncio.Semaphore(CONCURRENCY)

    async def one(window: StoredWindow) -> Trial:
        request = GateRequest(window.state, (question,), guard, Purpose.RULE_TEST, ask_mode=False)
        async with slots:
            try:
                answer = await gate.evaluate(request)
            except OverBudget as e:
                return Trial(window, None, str(e.refusal))
            except GateAuthError as e:
                await _record(pool, e.costs)
                raise
            except GateFailed as e:
                await _record(pool, e.costs)
                return Trial(window, None, e.reason)
        await _record(pool, answer.costs)
        return Trial(window, answer.probabilities[question.rule_id])

    # Let every call finish and record its cost before an error (a wrong key, a database
    # blip) propagates: a paid call must never miss the ledger.
    results = await asyncio.gather(*(one(w) for w in windows), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return [r for r in results if isinstance(r, Trial)]


async def _record(pool: Pool, costs: Sequence[ledger.CostEntry]) -> None:
    async with pool.connection() as conn:
        for cost in costs:
            await ledger.record(conn, cost)


def report(name: str, question: RuleQuestion, trials: Sequence[Trial], *, examples: int = 3) -> str:
    """The score spread, the pass rate, and the highest- and lowest-scoring Windows."""
    failed = [t for t in trials if t.probability is None]
    summary = summarize(question, trials, examples=examples)
    lines = [
        f"{name}: {summary.passed} of {summary.scored} Windows pass at {question.threshold}"
        + (f" ({len(failed)} not scored)" if failed else ""),
        "",
    ]
    buckets = summary.histogram
    width = max(buckets, default=0) or 1
    for i in reversed(range(10)):
        bar = "#" * round(buckets[i] / width * 30)
        lines.append(f"  {i / 10:.1f}-{(i + 1) / 10:.1f} {buckets[i]:4} {bar}")
    for title, chosen in (("Highest", summary.highest), ("Lowest", summary.lowest)):
        lines += ["", f"{title}:"]
        lines += [
            f"  {_where(t)}  {t.probability:.2f}  {_excerpt(t.window.state.new)}" for t in chosen
        ]
    if failed:
        lines += ["", "Not scored:"] + [f"  {_where(t)}  {t.error}" for t in failed[:examples]]
    return "\n".join(lines)


def _where(t: Trial) -> str:
    seconds = t.window.start_ms // 1000
    return f"session {t.window.session_id} {seconds // 60:3}:{seconds % 60:02}"


def _excerpt(text: str, chars: int = 100) -> str:
    return text if len(text) <= chars else text[: chars - 1] + "…"


@dataclass(frozen=True, slots=True)
class Summary:
    scored: int
    passed: int
    failed: int
    histogram: tuple[int, ...]
    highest: tuple[Trial, ...]
    lowest: tuple[Trial, ...]

    @property
    def pass_rate(self) -> float:
        return self.passed / self.scored if self.scored else 0.0


def summarize(question: RuleQuestion, trials: Sequence[Trial], *, examples: int = 3) -> Summary:
    scored = sorted(
        (t for t in trials if t.probability is not None),
        key=lambda t: t.probability or 0.0,
        reverse=True,
    )
    buckets = [0] * 10
    for t in scored:
        buckets[min(int((t.probability or 0.0) * 10), 9)] += 1
    return Summary(
        len(scored),
        sum(passes(t.probability or 0.0, question.threshold) for t in scored),
        len(trials) - len(scored),
        tuple(buckets),
        tuple(scored[:examples]),
        tuple(scored[-examples:][::-1]),
    )
