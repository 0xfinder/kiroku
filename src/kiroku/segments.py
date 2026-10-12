"""Category and Pack on the session audio clock, including after a restart."""

from dataclasses import dataclass

from kiroku.db import Pool
from kiroku.source import StreamMeta


@dataclass(frozen=True, slots=True)
class Segment:
    title: str
    category: str | None
    pack: str | None


async def record(
    pool: Pool,
    session_id: int,
    start_ms: int,
    meta: StreamMeta,
    pack: str | None,
    *,
    restart: bool = False,
) -> None:
    async with pool.connection() as conn, conn.transaction():
        if restart:
            # After a crash, uncommitted capture may have run beyond the resumed clock.
            await conn.execute(
                "DELETE FROM segment WHERE session_id = %s AND start_ms >= %s",
                [session_id, start_ms],
            )
        await conn.execute(
            """
            INSERT INTO segment (session_id, start_ms, title, category, pack)
            SELECT %(s)s, %(start)s, %(title)s, %(category)s, %(pack)s
            WHERE NOT EXISTS (
                SELECT 1 FROM segment WHERE session_id = %(s)s
                AND start_ms = (SELECT max(start_ms) FROM segment
                                WHERE session_id = %(s)s AND start_ms <= %(start)s)
                AND title = %(title)s AND category IS NOT DISTINCT FROM %(category)s
                AND pack IS NOT DISTINCT FROM %(pack)s
            )
            ON CONFLICT (session_id, start_ms) DO UPDATE
            SET title = EXCLUDED.title, category = EXCLUDED.category, pack = EXCLUDED.pack
            """,
            {
                "s": session_id,
                "start": start_ms,
                "title": meta.title,
                "category": meta.category,
                "pack": pack,
            },
        )


async def at(pool: Pool, session_id: int, start_ms: int) -> Segment | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT title, category, pack FROM segment
            WHERE session_id = %s AND start_ms <= %s ORDER BY start_ms DESC LIMIT 1
            """,
            [session_id, start_ms],
        )
        row = await cur.fetchone()
    return Segment(*row) if row else None
