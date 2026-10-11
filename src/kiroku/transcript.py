"""The stored transcript: the only writer of `utterance` and `transcript_span`.

One commit is one transaction: the span row, its utterances, and its cost_ledger row
(through kiroku.ledger, which stays the only writer of cost_ledger). The span's primary key
makes a repeated commit a no-op, so neither utterances nor cost can be stored twice.
"""

from __future__ import annotations

import dataclasses

from kiroku import ledger
from kiroku.db import Pool
from kiroku.transcriber import Result


async def commit(pool: Pool, session_id: int, transcriber: str, result: Result) -> bool:
    """Store one Result. Returns False when its span was already committed."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO transcript_span (session_id, transcriber, audio_start_ms, audio_end_ms)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1
            """,
            [session_id, transcriber, result.start_ms, result.end_ms],
        )
        if await cur.fetchone() is None:
            return False
        for u in result.utterances:
            # spoken_at comes from the capture leg holding the audio. No leg means a bug in
            # capture ordering: NULL fails the NOT NULL constraint instead of a made-up time.
            await conn.execute(
                """
                INSERT INTO utterance (session_id, audio_start_ms, audio_end_ms, spoken_at,
                                       speaker_label, text, transcriber)
                VALUES (%(s)s, %(start)s, %(end)s,
                        (SELECT l.started_at + (%(start)s - l.audio_start_ms) * interval '1 ms'
                         FROM capture_leg l
                         WHERE l.session_id = %(s)s AND l.audio_start_ms <= %(start)s
                         ORDER BY l.audio_start_ms DESC, l.seq DESC LIMIT 1),
                        %(speaker)s, %(text)s, %(transcriber)s)
                """,
                {
                    "s": session_id,
                    "start": u.start_ms,
                    "end": u.end_ms,
                    "speaker": u.speaker_label,
                    "text": u.text,
                    "transcriber": transcriber,
                },
            )
        if result.cost is not None:
            await ledger.record(conn, dataclasses.replace(result.cost, session_id=session_id))
        return True


async def tail_text(
    pool: Pool, session_id: int, transcriber: str, *, before_ms: int | None = None, chars: int = 400
) -> str:
    """The end of the stored transcript, or of the part before `before_ms`, to seed the
    prompt after a restart or ahead of a backfilled gap."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT coalesce(string_agg(text, ' ' ORDER BY audio_start_ms), '') FROM (
                SELECT text, audio_start_ms FROM utterance
                WHERE session_id = %s AND transcriber = %s
                  AND (%s::integer IS NULL OR audio_start_ms < %s)
                ORDER BY audio_start_ms DESC LIMIT 20) t
            """,
            [session_id, transcriber, before_ms, before_ms],
        )
        row = await cur.fetchone()
    return (row[0] if row else "")[-chars:]


async def spans(pool: Pool, session_id: int, transcriber: str) -> list[tuple[int, int]]:
    """Every committed (audio_start_ms, audio_end_ms) of this Transcriber, in order."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT audio_start_ms, audio_end_ms FROM transcript_span "
            "WHERE session_id = %s AND transcriber = %s ORDER BY audio_start_ms",
            [session_id, transcriber],
        )
        return list(await cur.fetchall())
