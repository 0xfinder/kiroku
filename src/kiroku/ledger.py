"""Cost ledger (D8): one append-only row per paid call, the source of truth for Budget caps.

This module is the only writer of cost_ledger. Spend is summed at read time; there is no
cached running total, so concurrent paid calls never contend on a shared counter.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from psycopg import AsyncConnection, sql

from kiroku.db import Pool, retrying


class Purpose(StrEnum):
    TRANSCRIBE = "transcribe"
    GATE = "gate"
    SPEAKER_MAP = "speaker_map"
    EXTRACTION = "extraction"
    NOTES = "notes"
    QA = "qa"
    RULE_TEST = "rule_test"


@dataclass(frozen=True, slots=True)
class Tokens:
    input: int
    output: int = 0


@dataclass(frozen=True, slots=True)
class AudioSeconds:
    seconds: Decimal


# A row is billed by tokens or by audio, never both or neither. The SQL CHECK mirrors this.
type Usage = Tokens | AudioSeconds


@dataclass(frozen=True, slots=True)
class CostEntry:
    provider: str
    model: str
    purpose: Purpose
    usage: Usage
    usd: Decimal
    latency_ms: int
    session_id: int | None = None


async def record(
    conn: AsyncConnection, entry: CostEntry, *, idempotency_key: UUID | None = None
) -> int:
    """Insert one row and return its id, inside the caller's transaction if any."""
    match entry.usage:
        case Tokens(input=i, output=o):
            input_tokens, output_tokens, audio = i, o, None
        case AudioSeconds(seconds=s):
            input_tokens, output_tokens, audio = None, None, s
    cur = await conn.execute(
        """
        INSERT INTO cost_ledger (provider, model, purpose, session_id, input_tokens,
                                 output_tokens, audio_seconds, usd, latency_ms, idempotency_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        [
            entry.provider,
            entry.model,
            entry.purpose.value,
            entry.session_id,
            input_tokens,
            output_tokens,
            audio,
            entry.usd,
            entry.latency_ms,
            idempotency_key,
        ],
    )
    row = await cur.fetchone()
    if row is None:
        row = await (
            await conn.execute(
                "SELECT id FROM cost_ledger WHERE idempotency_key = %s", [idempotency_key]
            )
        ).fetchone()
    assert row is not None
    return row[0]


async def record_session_costs(pool: Pool, session_id: int, costs: Sequence[CostEntry]) -> None:
    """Preserve billed attempts without advancing Gate progress, even after a lost commit ack."""
    entries = [(replace(cost, session_id=session_id), uuid4()) for cost in costs]

    async def write() -> None:
        async with pool.connection() as conn, conn.transaction():
            for entry, key in entries:
                await record(conn, entry, idempotency_key=key)

    await retrying(f"session {session_id}: Gate authentication costs", write)


async def spent(
    conn: AsyncConnection, *, since: datetime | None = None, session_id: int | None = None
) -> Decimal:
    """Total USD of matching rows, Decimal('0') when none.

    Monthly cap: `spent(conn, since=month_start)`. Session cap: `spent(conn, session_id=s)`.
    """
    if since is not None and since.tzinfo is None:
        raise ValueError("spent(since=...) needs a timezone-aware datetime")
    clauses: list[sql.Composable] = [sql.SQL("true")]
    params: list[object] = []
    if since is not None:
        clauses.append(sql.SQL("at >= %s"))
        params.append(since)
    if session_id is not None:
        clauses.append(sql.SQL("session_id = %s"))
        params.append(session_id)
    query = sql.SQL("SELECT coalesce(sum(usd), 0) FROM cost_ledger WHERE {}").format(
        sql.SQL(" AND ").join(clauses)
    )
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return row[0]
