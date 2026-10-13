"""The only writer of `label`. Commit each tap before acknowledging Telegram's update."""

import asyncio
import logging

import psycopg

from kiroku import alerting
from kiroku.db import Pool, connect
from kiroku.notify import Notifier, Tap

log = logging.getLogger(__name__)
TELEGRAM_LOCK_CLASS = 0x6B74


async def record(pool: Pool, tap: Tap) -> bool:
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT decision_id, outcome, external_id FROM alert WHERE id = %s FOR UPDATE",
            [tap.alert_id],
        )
        row = await cur.fetchone()
        if row is None or row[1] not in ("sending", "sent", "unconfirmed"):
            return False
        if row[2] is not None and row[2] != tap.delivery.external_id:
            return False
        await alerting.confirm_delivery(conn, tap.alert_id, tap.delivery, tap.sent_at)
        await conn.execute(
            """
            INSERT INTO label (decision_id, applies, source, alert_id)
            VALUES (%s, %s, 'telegram', %s)
            ON CONFLICT (decision_id, source) DO UPDATE
            SET applies = EXCLUDED.applies, alert_id = EXCLUDED.alert_id,
                labelled_at = CASE WHEN label.applies <> EXCLUDED.applies
                                   THEN now() ELSE label.labelled_at END
            """,
            [row[0], tap.applies, tap.alert_id],
        )
    return True


async def run_taps(pool: Pool, database_url: str, notifier: Notifier) -> None:
    while True:
        try:
            async with connect(database_url) as conn:
                waiting = False
                while True:
                    cur = await conn.execute(
                        "SELECT pg_try_advisory_lock(%s, 0)", [TELEGRAM_LOCK_CLASS]
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        break
                    if not waiting:
                        log.info("another kiroku process polls Telegram; waiting for ownership")
                        waiting = True
                    await asyncio.sleep(10.0)
                while True:
                    try:
                        async for tap in notifier.taps():
                            await conn.execute("SELECT 1")  # verify ownership connection is alive
                            recorded = await record(pool, tap)
                            await notifier.confirm(tap, recorded=recorded)
                    except psycopg.OperationalError:
                        raise  # reacquire the session lock on a new connection
                    except Exception as e:
                        log.warning("Label loop failed (%s); awaiting redelivery", type(e).__name__)
                        await asyncio.sleep(5.0)
        except Exception as e:
            log.warning("Label connection failed (%s); retrying", type(e).__name__)
            await asyncio.sleep(5.0)


async def record_review(pool: Pool, decision_id: int, applies: bool) -> bool:
    """Latest Review verdict wins. Telegram's verdict remains independent."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO label(decision_id,applies,source) "
            "SELECT id,%s,'review' FROM gate_decision WHERE id=%s "
            "ON CONFLICT(decision_id,source) DO UPDATE "
            "SET applies=excluded.applies,labelled_at=now() RETURNING id",
            [applies, decision_id],
        )
        return await cur.fetchone() is not None


async def undo_review(pool: Pool, decision_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM label WHERE decision_id=%s AND source='review'",
            [decision_id],
        )
