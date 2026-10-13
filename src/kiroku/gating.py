"""Gate decisions: the only writer of `gate_window` and `gate_decision`, and the per-session
loop that closes Windows and asks the Gate about them.

One commit is one transaction: the Window row (with the exact state the Gate saw), one
decision per Rule asked, and the Gate's Cost ledger rows. The (session_id, start_ms) key makes
a repeated commit a no-op, so neither decisions nor cost can be stored twice. Every Window
ends answered, empty, over_budget or failed, and every status writes one decision per Rule
(probability NULL when the Rule wasn't asked), so every (Window, Rule) pair has exactly one row.

The loop's inputs all come from the database: the transcript watermark, utterances, Rules,
and the last Window's end. A restart therefore resumes exactly where it stopped; the wake-up
after each transcript commit is only a latency shortcut.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

from kiroku import ledger, rules, segments, transcript
from kiroku.budget import BudgetGuard, OverBudget
from kiroku.db import Pool, retrying
from kiroku.gate import (
    Gate,
    GateAnswer,
    GateAuthError,
    GateFailed,
    GateRequest,
    Mode,
    RuleQuestion,
    passes,
    rule_question,
    stored_probability,
)
from kiroku.gate.windows import (
    JSON,
    Bounds,
    GateState,
    Turn,
    WindowPolicy,
    build_state,
    due_windows,
)
from kiroku.ledger import CostEntry
from kiroku.llm import LLMDatabaseError
from kiroku.pack import LiveContext

log = logging.getLogger(__name__)

POLL_S = 10.0  # backstop when no transcript commit wakes the loop


class Status(StrEnum):
    ANSWERED = "answered"
    EMPTY = "empty"
    OVER_BUDGET = "over_budget"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Answered:
    answer: GateAnswer


@dataclass(frozen=True, slots=True)
class NotAsked:
    status: Literal[Status.EMPTY, Status.OVER_BUDGET, Status.FAILED]
    reason: str | None
    costs: tuple[CostEntry, ...] = ()  # a failed call may have been billed


type Outcome = Answered | NotAsked


async def watermark(pool: Pool, session_id: int) -> int:
    """End of persisted Gate Windows, including terminal failures."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT coalesce(max(end_ms), 0) FROM gate_window WHERE session_id=%s", [session_id]
            )
        ).fetchone()
    assert row is not None
    return row[0]


async def progress(pool: Pool, session_id: int, transcriber: str) -> tuple[int, int]:
    """(where the next Window starts, the transcript watermark)."""
    return await watermark(pool, session_id), await transcript.watermark(
        pool, session_id, transcriber
    )


async def ungated_closed_sessions(
    pool: Pool, channel_id: int, transcriber: str
) -> list[tuple[int, str | None]]:
    """Closed sessions of a channel whose transcript runs past their last Window: their final
    pass failed, or the stream ended while kiroku was down. (id, title), oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT s.id, s.title FROM stream_session s
            WHERE s.channel_id = %(c)s AND s.ended_at IS NOT NULL
              AND (SELECT max(audio_end_ms) FROM transcript_span t
                   WHERE t.session_id = s.id AND t.transcriber = %(t)s)
                  > (SELECT coalesce(max(end_ms), 0) FROM gate_window w WHERE w.session_id = s.id)
            ORDER BY s.id
            """,
            {"c": channel_id, "t": transcriber},
        )
        return list(await cur.fetchall())


async def turns(
    pool: Pool, session_id: int, transcriber: str, from_ms: int, to_ms: int
) -> list[Turn]:
    """Utterances starting in [from_ms, to_ms), in order. Every one counts until a Speaker map
    exists; it will add a join here and nothing elsewhere."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT audio_start_ms, text FROM utterance
            WHERE session_id = %s AND transcriber = %s
              AND audio_start_ms >= %s AND audio_start_ms < %s
            ORDER BY audio_start_ms
            """,
            [session_id, transcriber, from_ms, to_ms],
        )
        rows = await cur.fetchall()
    return [Turn(start, text) for start, text in rows]


async def commit(
    pool: Pool,
    session_id: int,
    transcriber: str,
    bounds: Bounds,
    state: GateState,
    questions: Sequence[RuleQuestion],
    outcome: Outcome,
    game_key: str | None = None,
) -> bool:
    """Store one gated Window. Returns False when it was already stored (nothing written)."""
    match outcome:
        case Answered(answer):
            status, reason, costs = Status.ANSWERED, answer.note, answer.costs
            model, mode = answer.model, answer.mode
            mode_p = _decimal(answer.mode_probability)
            probabilities: Mapping[int, float] = answer.probabilities
        case NotAsked(status, reason, costs):
            model = mode = mode_p = None
            probabilities = {}
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO gate_window (session_id, start_ms, end_ms, transcriber, started_at,
                                     covered_ms, state, status, reason, model, mode,
                                     mode_probability, game_key)
            VALUES (%(s)s, %(start)s, %(end)s, %(t)s,
                    (SELECT l.started_at + (%(start)s - l.audio_start_ms) * interval '1 ms'
                     FROM capture_leg l
                     WHERE l.session_id = %(s)s AND l.audio_start_ms <= %(start)s
                     ORDER BY l.audio_start_ms DESC, l.seq DESC LIMIT 1),
                    -- Spans of one Transcriber don't overlap; least() keeps a bug there from
                    -- failing the CHECK, and with it every later Window.
                    (SELECT least(coalesce(sum(least(audio_end_ms, %(end)s)
                                               - greatest(audio_start_ms, %(start)s)), 0),
                                  %(end)s - %(start)s)
                     FROM transcript_span
                     WHERE session_id = %(s)s AND transcriber = %(t)s
                       AND audio_start_ms < %(end)s AND audio_end_ms > %(start)s),
                    %(state)s, %(status)s, %(reason)s, %(model)s, %(mode)s, %(mode_p)s,
                    %(game_key)s)
            ON CONFLICT (session_id, start_ms) DO NOTHING
            RETURNING id
            """,
            {
                "s": session_id,
                "start": bounds.start_ms,
                "end": bounds.end_ms,
                "t": transcriber,
                "state": Jsonb(state.as_json()),
                "status": status.value,
                "reason": reason,
                "model": model,
                "mode": mode.value if mode else None,
                "mode_p": mode_p,
                "game_key": game_key,
            },
        )
        if (row := await cur.fetchone()) is None:
            return False
        for q in questions:
            p = _decimal(probabilities.get(q.rule_id))
            await conn.execute(
                """
                INSERT INTO gate_decision (window_id, rule_id, question, threshold, probability)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [row[0], q.rule_id, q.question, q.threshold, p],
            )
        for cost in costs:
            await ledger.record(conn, dataclasses.replace(cost, session_id=session_id))
    return True


def _decimal(p: float | None) -> Decimal | None:
    return None if p is None else stored_probability(p)


class GateLoop:
    """Closes a session's Windows as its transcript completes and gates each one, in order."""

    def __init__(
        self,
        pool: Pool,
        session_id: int,
        transcriber: str,
        gate: Gate,
        guard: BudgetGuard,
        stream: Callable[[], Mapping[str, JSON]],
        *,
        live: Callable[[Bounds], LiveContext | None] = lambda bounds: None,
        use_segments: bool = False,
        blocked_from: Callable[[], int | None] = lambda: None,
        policy: WindowPolicy | None = None,
        poll_s: float = POLL_S,
        on_commit: Callable[[], None] = lambda: None,
        on_disabled: Callable[[int], None] = lambda session: None,
    ) -> None:
        self._on_commit = on_commit
        self._on_disabled = on_disabled
        self._pool = pool
        self._session_id = session_id
        self._transcriber = transcriber
        self._gate = gate
        self._guard = guard
        self._stream = stream
        self._live = live
        self._use_segments = use_segments
        self._blocked_from = blocked_from
        self._policy = policy or WindowPolicy()
        self._poll_s = poll_s
        self._wake = asyncio.Event()
        self._finishing: bool | None = None  # set by finish(): whether the session ended

    def wake(self) -> None:
        self._wake.set()

    def finish(self, *, session_ended: bool) -> None:
        """Make run() do one last pass and return. With `session_ended`, that pass also closes
        the trailing partial Window; otherwise a restart closes it at full length."""
        self._finishing = session_ended
        self._wake.set()

    async def run(self) -> None:
        while True:
            self._wake.clear()
            finishing = self._finishing
            try:
                await self._pass(final=finishing is True)
            except psycopg.OperationalError as e:
                # Nothing was asked yet for the Window being prepared, so nothing is lost.
                log.warning("session %d: database error in Gate (%s)", self._session_id, e)
                if finishing is not None:
                    # The last pass must still happen; the caller bounds how long it waits.
                    await asyncio.sleep(1.0)
                    continue
            except GateAuthError as e:
                # The transcript is the part that can't be recovered later, so it keeps going.
                # A restart with a fixed key gates the backlog from where this stopped.
                log.error("session %d: %s; the Gate is off for this session", self._session_id, e)
                self._on_disabled(self._session_id)
                return
            except Exception:
                # Likewise for a bug: log it loudly, but never take transcription down with it.
                log.exception(
                    "session %d: Gate crashed; it is off for this session", self._session_id
                )
                self._on_disabled(self._session_id)
                return
            if finishing is not None:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._poll_s)

    async def _pass(self, *, final: bool) -> None:
        next_start, watermark = await progress(self._pool, self._session_id, self._transcriber)
        for bounds in due_windows(next_start, watermark, self._policy, final=final):
            pending = self._blocked_from()
            if pending is not None and bounds.start_ms >= pending:
                return  # the original metadata boundary is being retried in the watcher
            window_turns = await turns(
                self._pool,
                self._session_id,
                self._transcriber,
                bounds.start_ms - self._policy.context_lookback_ms,
                bounds.end_ms,
            )
            segment = (
                await segments.at(self._pool, self._session_id, bounds.start_ms)
                if self._use_segments
                else None
            )
            stream = self._stream()
            pack = None
            if segment is not None:
                stream = {"title": segment.title}
                if segment.category:
                    stream["category"] = segment.category
                pack = segment.pack
            elif self._use_segments:
                stream = {"title": stream.get("title", "")}
                pack = None  # no historical Category evidence in pre-feature sessions
            state = build_state(bounds, window_turns, stream, self._policy)
            live = self._live(bounds)
            if live is not None:
                state = GateState({**state.context, "live": dict(live.summary)}, state.new)
            questions = [rule_question(r) for r in await rules.active(self._pool, pack)]
            try:
                outcome = await self._ask(state, questions)
            except (GateAuthError, LLMDatabaseError) as e:
                if e.costs:
                    await ledger.record_session_costs(self._pool, self._session_id, e.costs)
                raise
            game_key = (
                live.game_key
                if live is not None
                and isinstance(outcome, Answered)
                and outcome.answer.mode == Mode.WATCHING_PRO
                else None
            )
            await self._commit(bounds, state, questions, outcome, game_key)
            _log_window(self._session_id, bounds, questions, outcome)

    async def _ask(self, state: GateState, questions: Sequence[RuleQuestion]) -> Outcome:
        if not state.new:
            return NotAsked(Status.EMPTY, None)
        request = GateRequest(state, tuple(questions), self._guard)
        try:
            return Answered(await self._gate.evaluate(request))
        except OverBudget as e:
            return NotAsked(Status.OVER_BUDGET, str(e.refusal), e.costs)
        except GateFailed as e:
            if not e.reported:
                log.error("session %d: Gate failed: %s", self._session_id, e.reason)
            return NotAsked(Status.FAILED, e.reason, e.costs)

    async def _commit(
        self,
        bounds: Bounds,
        state: GateState,
        questions: Sequence[RuleQuestion],
        outcome: Outcome,
        game_key: str | None = None,
    ) -> None:
        """Retry the commit, never the paid call that produced the outcome."""
        await retrying(
            f"session {self._session_id}: Gate commit",
            lambda: commit(
                self._pool,
                self._session_id,
                self._transcriber,
                bounds,
                state,
                questions,
                outcome,
                game_key,
            ),
        )
        self._on_commit()


def _log_window(
    session_id: int, bounds: Bounds, questions: Sequence[RuleQuestion], outcome: Outcome
) -> None:
    span = f"{bounds.start_ms / 1000:.0f}-{bounds.end_ms / 1000:.0f} s"
    match outcome:
        case Answered(answer):
            scores = ", ".join(
                f"{q.name} {p:.2f}" + (" PASS" if passes(p, q.threshold) else "")
                for q in questions
                for p in [answer.probabilities[q.rule_id]]
            )
            log.info("session %d: window %s: %s; %s", session_id, span, answer.mode, scores or "-")
        case NotAsked(status, reason):
            log.info("session %d: window %s: %s %s", session_id, span, status, reason or "")
