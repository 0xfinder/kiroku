from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation

from kiroku.ledger import AudioSeconds, CostEntry, Purpose, Tokens, record, spent


async def _session(conn: AsyncConnection) -> int:
    cur = await conn.execute(
        """
        WITH ch AS (INSERT INTO channel (platform, login) VALUES ('twitch', 'gorgc') RETURNING id)
        INSERT INTO stream_session (channel_id, started_at) SELECT id, now() FROM ch RETURNING id
        """
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def test_spent_sums_exactly_by_session_and_time(conn: AsyncConnection) -> None:
    session_id = await _session(conn)
    groq = CostEntry(
        "groq",
        "whisper-large-v3-turbo",
        Purpose.TRANSCRIBE,
        AudioSeconds(Decimal("30")),
        Decimal("0.000333"),
        480,
        session_id,
    )
    jev = CostEntry("jev", "jev-1.13.0", Purpose.RULE_TEST, Tokens(700), Decimal("0.000029"), 300)
    await record(conn, groq)
    await record(conn, jev)

    assert await spent(conn, session_id=session_id) == Decimal("0.000333")
    assert await spent(conn, since=datetime.now(UTC) - timedelta(days=1)) == Decimal("0.000362")
    assert await spent(conn, since=datetime.now(UTC) + timedelta(days=1)) == Decimal("0")


async def test_row_without_usage_is_rejected_by_the_database(conn: AsyncConnection) -> None:
    with pytest.raises(CheckViolation):
        await conn.execute(
            "INSERT INTO cost_ledger (provider, model, purpose, usd, latency_ms) "
            "VALUES ('x', 'y', 'gate', 0, 1)"
        )


async def test_naive_since_is_rejected(conn: AsyncConnection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await spent(conn, since=datetime(2026, 11, 1))
