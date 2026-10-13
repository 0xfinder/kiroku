"""The only writer of `alert`. Durable decisions, ordered per channel, with at-most-once sends."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from kiroku import extraction, ledger, sessions
from kiroku.budget import Budget, OverBudget
from kiroku.db import Pool, retrying
from kiroku.extraction import Previous
from kiroku.gate.windows import JSON, GateState
from kiroku.ledger import CostEntry
from kiroku.llm import LLM, LLMAuthError, LLMDatabaseError, LLMFailed
from kiroku.notify import (
    AlertMessage,
    Delivery,
    Notifier,
    NotifierRejected,
    NotifierStale,
    NotifierUnavailable,
    NotifierUnsure,
)

log = logging.getLogger(__name__)
type Content = Mapping[str, JSON]


@dataclass(frozen=True, slots=True)
class Candidate:
    decision_id: int
    rule_id: int
    probability: Decimal
    session_id: int
    channel_id: int
    channel: str
    start_ms: int
    end_ms: int
    said_at: datetime | None
    said_until: datetime | None
    state: GateState
    game_key: str | None
    gate_model: str
    segment_ms: int
    rule: str
    question: str
    cooldown_s: int


@dataclass(frozen=True, slots=True)
class Stale:
    content: Content | None = None
    costs: tuple[CostEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class Cooldown:
    game_key: str
    last_alert_id: int


@dataclass(frozen=True, slots=True)
class Duplicate:
    game_key: str
    content: Content
    previous_id: int
    model: str
    costs: tuple[CostEntry, ...]


@dataclass(frozen=True, slots=True)
class Sending:
    game_key: str
    content: Content
    previous_id: int | None
    model: str | None
    notifier: str
    costs: tuple[CostEntry, ...]


@dataclass(frozen=True, slots=True)
class Failed:
    game_key: str
    content: Content
    notifier: str
    reason: str


type Decided = Stale | Cooldown | Duplicate | Sending | Failed


async def pending(pool: Pool, channel_ids: Sequence[int]) -> list[Candidate]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            sql.SQL("""
            SELECT d.id, d.rule_id, d.probability, w.session_id, s.channel_id, c.login,
                   w.start_ms, w.end_ms, w.started_at, speech.said_until,
                   w.state, w.game_key, w.model,
                   coalesce((SELECT max(g.start_ms) FROM segment g
                             WHERE g.session_id = w.session_id AND g.start_ms <= w.start_ms), 0),
                   r.name, d.question, r.cooldown_s
            FROM gate_decision d
            JOIN gate_window w ON w.id = d.window_id
            JOIN stream_session s ON s.id = w.session_id
            JOIN channel c ON c.id = s.channel_id
            JOIN rule r ON r.id = d.rule_id
            LEFT JOIN LATERAL ({speech}) speech ON true
            WHERE d.passed AND s.channel_id = ANY(%s)
              AND NOT EXISTS (SELECT 1 FROM alert a WHERE a.decision_id = d.id)
            ORDER BY w.started_at NULLS FIRST, w.session_id, w.start_ms, d.rule_id LIMIT 20
            """).format(
                speech=sessions.said_until_sql(sql.SQL("w.session_id"), sql.SQL("w.end_ms"))
            ),
            [list(channel_ids)],
        )
        rows = await cur.fetchall()
    out: list[Candidate] = []
    for (
        d,
        r,
        p,
        s,
        c,
        channel,
        start,
        end,
        said,
        until,
        raw,
        key,
        model,
        segment,
        name,
        q,
        cd,
    ) in rows:
        state = cast(dict[str, Any], raw)
        out.append(
            Candidate(
                d,
                r,
                p,
                s,
                c,
                channel,
                start,
                end,
                said,
                until,
                GateState(state["context"], state["new"]),
                key,
                model,
                segment,
                name,
                q,
                cd,
            )
        )
    return out


def coarse_game_key(
    session_id: int,
    segment_ms: int,
    window_start_ms: int,
    said_at: datetime,
    chain: tuple[str, datetime] | None,
    gap: timedelta,
) -> str:
    prefix = f"seg:{session_id}:{segment_ms}:"
    if chain and chain[0].startswith(prefix) and timedelta(0) <= said_at - chain[1] <= gap:
        return chain[0]
    return f"{prefix}{window_start_ms}"


async def game_key(pool: Pool, candidate: Candidate, gap: timedelta) -> str:
    if candidate.game_key is not None:
        return candidate.game_key
    assert candidate.said_at is not None
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT game_key, said_at FROM alert
            WHERE rule_id = %s AND channel_id = %s AND game_key LIKE %s
              AND said_at <= %s ORDER BY said_at DESC, id DESC LIMIT 1
            """,
            [
                candidate.rule_id,
                candidate.channel_id,
                f"seg:{candidate.session_id}:{candidate.segment_ms}:%",
                candidate.said_at,
            ],
        )
        row = await cur.fetchone()
    return coarse_game_key(
        candidate.session_id,
        candidate.segment_ms,
        candidate.start_ms,
        candidate.said_at,
        (row[0], row[1]) if row else None,
        gap,
    )


async def in_cooldown(pool: Pool, candidate: Candidate) -> int | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id FROM alert
            WHERE rule_id = %s AND channel_id = %s AND outcome IN ('sending', 'sent', 'unconfirmed')
              AND said_at <= %s AND said_at > %s - make_interval(secs => %s)
            ORDER BY said_at DESC, id DESC LIMIT 1
            """,
            [
                candidate.rule_id,
                candidate.channel_id,
                candidate.said_at,
                candidate.said_at,
                candidate.cooldown_s,
            ],
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def previous(pool: Pool, candidate: Candidate, key: str) -> Previous | None:
    assert candidate.said_at is not None
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, content->>'claim', said_at FROM alert
            WHERE rule_id = %s AND channel_id = %s AND game_key = %s
              AND outcome IN ('sent', 'unconfirmed') AND content->>'claim' IS NOT NULL
              AND said_at <= %s ORDER BY said_at DESC, id DESC LIMIT 1
            """,
            [candidate.rule_id, candidate.channel_id, key, candidate.said_at],
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return Previous(row[0], row[1], int((candidate.said_at - row[2]).total_seconds() // 60))


async def commit(pool: Pool, candidate: Candidate, decided: Decided) -> int | None:
    key = content = model = previous_id = notifier = reason = None
    costs: tuple[CostEntry, ...] = ()
    match decided:
        case Stale(content, costs):
            outcome = "stale"
        case Cooldown(key, last_alert_id):
            outcome, reason = "cooldown", f"Cooldown after alert {last_alert_id}"
        case Duplicate(key, content, previous_id, model, costs):
            outcome = "duplicate"
        case Sending(key, content, previous_id, model, notifier, costs):
            outcome = "sending"
            degraded = content.get("degraded")
            reason = degraded if isinstance(degraded, str) else None
        case Failed(key, content, notifier, reason):
            outcome = "failed"
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO alert (decision_id, rule_id, session_id, channel_id, said_at,
                               game_key, outcome, reason, content, extraction_model,
                               previous_alert_id, notifier)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (decision_id) DO NOTHING RETURNING id
            """,
            [
                candidate.decision_id,
                candidate.rule_id,
                candidate.session_id,
                candidate.channel_id,
                candidate.said_at,
                key,
                outcome,
                reason,
                Jsonb(content) if content is not None else None,
                model,
                previous_id,
                notifier,
            ],
        )
        row = await cur.fetchone()
        if row is None:
            return None
        for cost in costs:
            await ledger.record(conn, replace(cost, session_id=candidate.session_id))
        return row[0]


async def mark_sent(pool: Pool, alert_id: int, delivery: Delivery, at: datetime) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE alert SET outcome = 'sent', external_id = %s, sent_at = %s "
            "WHERE id = %s AND outcome = 'sending'",
            [delivery.external_id, at, alert_id],
        )


async def mark_stale(pool: Pool, alert_id: int, reason: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE alert SET outcome = 'stale', notifier = NULL, reason = %s "
            "WHERE id = %s AND outcome = 'sending'",
            [reason, alert_id],
        )


async def mark_failed(pool: Pool, alert_id: int, reason: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE alert SET outcome = 'failed', reason = %s "
            "WHERE id = %s AND outcome = 'sending'",
            [reason, alert_id],
        )


async def mark_unconfirmed(pool: Pool, alert_id: int, reason: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE alert SET outcome = 'unconfirmed', reason = %s "
            "WHERE id = %s AND outcome = 'sending'",
            [reason, alert_id],
        )


async def confirm_delivery(
    conn: AsyncConnection, alert_id: int, delivery: Delivery, at: datetime
) -> None:
    """Called by Labels inside the transaction holding the alert's row lock."""
    await conn.execute(
        "UPDATE alert SET outcome = 'sent', external_id = %s, sent_at = %s "
        "WHERE id = %s AND outcome IN ('sending', 'unconfirmed')",
        [delivery.external_id, at, alert_id],
    )


async def recover(pool: Pool, channel_ids: Sequence[int]) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE alert SET outcome = 'unconfirmed', reason = 'process stopped during send' "
            "WHERE outcome = 'sending' AND channel_id = ANY(%s)",
            [list(channel_ids)],
        )
        return cur.rowcount


async def recent(
    pool: Pool, last: int
) -> list[tuple[datetime | None, str, str, str, str | None, str | None]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT a.said_at, c.login, r.name, a.outcome, a.reason, a.content->>'claim'
            FROM alert a JOIN channel c ON c.id = a.channel_id JOIN rule r ON r.id = a.rule_id
            ORDER BY a.id DESC LIMIT %s
            """,
            [last],
        )
        return list(await cur.fetchall())


class AlertWorker:
    def __init__(
        self,
        pool: Pool,
        channel_ids: Sequence[int],
        llm: LLM,
        notifier: Notifier,
        budget: Budget,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_after: timedelta = timedelta(minutes=10),
        coarse_gap: timedelta = timedelta(minutes=20),
        poll_s: float = 10.0,
    ) -> None:
        self._pool, self._channels, self._llm = pool, channel_ids, llm
        self._notifier, self._budget, self._now = notifier, budget, now
        self._stale_after, self._coarse_gap, self._poll_s = stale_after, coarse_gap, poll_s
        self._wake = asyncio.Event()
        self._finishing = False
        self._errors: set[str] = set()

    def wake(self) -> None:
        self._wake.set()

    def finish(self) -> None:
        """Do a final drain after all channels have committed their final Gate passes."""
        self._finishing = True
        self.wake()

    def _stale(self, candidate: Candidate) -> bool:
        return (
            candidate.said_at is None
            or candidate.said_until is None
            or self._now() - candidate.said_until > self._stale_after
        )

    async def run(self) -> None:
        try:
            await retrying("Alert database write", lambda: recover(self._pool, self._channels))
            while True:
                self._wake.clear()
                finishing = self._finishing
                try:
                    await self.drain()
                except psycopg.OperationalError:
                    log.warning("Alert database read failed; retrying")
                if finishing:
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._poll_s)
        except Exception as e:
            log.error(
                "Alert worker crashed (%s); alerts are off for this process", type(e).__name__
            )

    async def drain(self) -> None:
        """Handle all currently pending decisions, also used by the offline replay."""
        while candidates := await pending(self._pool, self._channels):
            for candidate in candidates:
                try:
                    await self._one(candidate)
                except psycopg.OperationalError:
                    raise
                except Exception as e:
                    log.error(
                        "Alert decision %d crashed (%s)", candidate.decision_id, type(e).__name__
                    )
                    await self._bug(candidate, f"bug: {type(e).__name__}")

    async def _bug(self, candidate: Candidate, reason: str) -> None:
        # A row already in sending may have reached Telegram before the exception.
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT id, outcome FROM alert WHERE decision_id = %s", [candidate.decision_id]
                )
            ).fetchone()
        if row is not None:
            if row[1] == "sending":
                await retrying(
                    "Alert database write", lambda: mark_unconfirmed(self._pool, row[0], reason)
                )
            return
        if candidate.said_at is None or candidate.said_until is None:
            await self._commit(candidate, Stale())
            return
        key = await game_key(self._pool, candidate, self._coarse_gap)
        await self._commit(
            candidate,
            Failed(
                key,
                {"degraded": reason, "excerpt": candidate.state.new[-280:]},
                self._notifier.name,
                reason,
            ),
        )

    async def _commit(self, candidate: Candidate, decided: Decided) -> int | None:
        return await retrying(
            "Alert database write", lambda: commit(self._pool, candidate, decided)
        )

    async def _one(self, candidate: Candidate) -> None:
        if self._stale(candidate):
            await self._commit(candidate, Stale())
            return
        key = await game_key(self._pool, candidate, self._coarse_gap)
        last = await in_cooldown(self._pool, candidate)
        if last is not None:
            await self._commit(candidate, Cooldown(key, last))
            return
        prev = await previous(self._pool, candidate, key)
        model: str | None = None
        same = False
        costs: tuple[CostEntry, ...] = ()
        try:
            result = await extraction.extract(
                self._llm,
                self._budget.guard(candidate.session_id),
                candidate.question,
                candidate.channel,
                candidate.state,
                prev,
            )
            model, costs = result.model, result.costs
            value = result.value
            content: Content = {
                "claim": value.claim,
                "summary": value.summary,
                "quote": value.quote,
            }
            same = value.same_as_previous
        except LLMDatabaseError as e:
            await ledger.record_session_costs(self._pool, candidate.session_id, e.costs)
            raise
        except (LLMFailed, OverBudget, LLMAuthError) as e:
            reason = e.reason if isinstance(e, LLMFailed) else str(e)
            costs = e.costs
            content = {"degraded": reason, "excerpt": candidate.state.new[-280:]}
            if isinstance(e, LLMAuthError) and reason not in self._errors:
                self._errors.add(reason)
                log.error("Extraction: %s", reason)
        if self._stale(candidate):
            await self._commit(candidate, Stale(content, costs))
            return
        if same and prev is not None and model is not None:
            await self._commit(candidate, Duplicate(key, content, prev.alert_id, model, costs))
            return
        decided = Sending(
            key, content, prev.alert_id if prev else None, model, self._notifier.name, costs
        )
        await self._deliver(candidate, decided)

    async def _deliver(self, candidate: Candidate, decided: Sending) -> None:
        assert candidate.said_at is not None and candidate.said_until is not None
        left = (candidate.said_until + self._stale_after - self._now()).total_seconds()
        if left < 5.0:
            await self._commit(candidate, Stale(decided.content, decided.costs))
            return
        alert_id = await self._commit(candidate, decided)
        if alert_id is None:
            return
        content = decided.content
        degraded = content.get("degraded")
        quote = content.get("quote")
        summary = content.get("summary", content.get("excerpt", ""))
        assert isinstance(summary, str)
        message = AlertMessage(
            alert_id,
            candidate.channel,
            candidate.rule,
            summary,
            quote if isinstance(quote, str) else None,
            candidate.said_at,
            candidate.probability,
            candidate.gate_model,
            degraded if isinstance(degraded, str) else None,
        )
        left = (candidate.said_until + self._stale_after - self._now()).total_seconds()
        try:
            delivery = await self._notifier.send(message, give_up_s=min(120.0, max(0.0, left)))
        except NotifierStale as e:
            reason = str(e)
            await retrying("Alert database write", lambda: mark_stale(self._pool, alert_id, reason))
        except (NotifierUnavailable, NotifierRejected) as e:
            reason = str(e)
            if reason not in self._errors:
                self._errors.add(reason)
                log.error("Alert delivery failed: %s", reason)
            await retrying(
                "Alert database write", lambda: mark_failed(self._pool, alert_id, reason)
            )
        except NotifierUnsure as e:
            reason = str(e)
            await retrying(
                "Alert database write", lambda: mark_unconfirmed(self._pool, alert_id, reason)
            )
        else:
            await retrying(
                "Alert database write",
                lambda: mark_sent(self._pool, alert_id, delivery, self._now()),
            )


async def session_rows(pool: Pool, session_id: int, limit: int) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT a.id,a.decision_id,r.name AS rule,a.outcome,a.said_at,w.start_ms,"
            "coalesce(a.content->>'claim',a.content->>'summary',a.content->>'excerpt','') AS text,"
            "(SELECT l.applies FROM label l WHERE l.decision_id=a.decision_id "
            "AND l.source='telegram' LIMIT 1) AS label "
            "FROM alert a JOIN gate_decision d ON d.id=a.decision_id "
            "JOIN gate_window w ON w.id=d.window_id JOIN rule r ON r.id=a.rule_id "
            "WHERE a.session_id=%s ORDER BY a.id DESC LIMIT %s",
            [session_id, limit],
        )
        names = [col.name for col in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in reversed(await cur.fetchall())]


async def count_for_session(pool: Pool, session_id: int) -> int:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) FROM alert WHERE session_id=%s",
                [session_id],
            )
        ).fetchone()
    assert row is not None
    return row[0]
