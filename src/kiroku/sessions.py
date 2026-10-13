"""Channels, Stream sessions and capture legs: the only writer of `channel`,
`stream_session` and `capture_leg`.

A session is one broadcast (the platform's stream id). Opening converges whatever state a
crash, a restart or a Twitch blip left behind: the same broadcast always maps to the same
session, and a new broadcast closes the stale one first.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from psycopg import Error, sql

from kiroku.db import Pool, connect
from kiroku.errors import KirokuError
from kiroku.source import StreamMeta

WATCH_LOCK_CLASS = 0x6B72  # advisory-lock namespace: one `kiroku watch` per channel


class ChannelBusy(KirokuError):
    pass


class UnsealedLeg(RuntimeError):
    """A leg was about to be placed while an earlier one has no end (a bug: seal first)."""


@dataclass(frozen=True, slots=True)
class Channel:
    id: int
    platform: str
    login: str


@dataclass(frozen=True, slots=True)
class Leg:
    session_id: int
    seq: int
    audio_start_ms: int
    archive_path: PurePosixPath  # relative to the data directory
    audio_end_ms: int | None = None  # None while capturing, or after a crash until sealed


@dataclass(frozen=True, slots=True)
class SessionInfo:
    id: int
    title: str


@asynccontextmanager
async def claim_channels(
    database_url: str, platform: str, logins: Sequence[str]
) -> AsyncGenerator[list[Channel]]:
    """Upsert the channels and hold an advisory lock on each for the life of the block.

    Two watchers on one channel would write the same session twice. The locks live on a
    dedicated connection (session locks belong to a connection). Cleanup waits for an
    explicit unlock; closing a socket alone can return before Postgres releases its locks.
    """
    # Twitch logins are case-insensitive; dedupe so one process never watches a channel twice
    # (session advisory locks are re-entrant on their own connection).
    unique = list(dict.fromkeys(login.lower() for login in logins))
    async with connect(database_url) as conn:
        channels: list[Channel] = []
        try:
            for login in unique:
                cur = await conn.execute(
                    """
                    INSERT INTO channel (platform, login) VALUES (%s, %s)
                    ON CONFLICT (platform, login) DO UPDATE SET enabled = channel.enabled
                    RETURNING id
                    """,
                    [platform, login],
                )
                row = await cur.fetchone()
                assert row is not None
                cur = await conn.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)", [WATCH_LOCK_CLASS, row[0]]
                )
                locked = await cur.fetchone()
                if not (locked and locked[0]):
                    raise ChannelBusy(f"another `kiroku watch` is already watching {login}")
                channels.append(Channel(row[0], platform, login))
            yield channels
        finally:
            with suppress(Error):
                await conn.execute("SELECT pg_advisory_unlock_all()")


async def open_session(pool: Pool, channel: Channel, meta: StreamMeta, now: datetime) -> int:
    """The session for this broadcast, created or reopened. Idempotent.

    In one transaction, with the channel row locked:
      1. an open session for this stream id -> return it
      2. an open session for another stream id -> close it
      3. the latest closed session for this stream id -> reopen it, so a blip or a restart
         within one broadcast stays one session
      4. otherwise insert a new session
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM channel WHERE id = %s FOR UPDATE", [channel.id])
        cur = await conn.execute(
            "SELECT id, platform_stream_id FROM stream_session "
            "WHERE channel_id = %s AND ended_at IS NULL",
            [channel.id],
        )
        if (open_row := await cur.fetchone()) is not None:
            if open_row[1] == meta.stream_id:
                return open_row[0]
            await conn.execute(
                "UPDATE stream_session SET ended_at = greatest(%s, started_at) WHERE id = %s",
                [now, open_row[0]],
            )
        cur = await conn.execute(
            """
            UPDATE stream_session SET ended_at = NULL, title = %s
            WHERE id = (SELECT id FROM stream_session
                        WHERE channel_id = %s AND platform_stream_id = %s
                        ORDER BY started_at DESC LIMIT 1)
            RETURNING id
            """,
            [meta.title, channel.id, meta.stream_id],
        )
        if (reopened := await cur.fetchone()) is not None:
            return reopened[0]
        cur = await conn.execute(
            """
            INSERT INTO stream_session (channel_id, platform_stream_id, title, started_at)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            [channel.id, meta.stream_id, meta.title, now],
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0]


async def close_open_sessions(pool: Pool, channel: Channel, now: datetime) -> None:
    """Close whatever session of the channel is open. Safe to repeat."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE stream_session SET ended_at = greatest(%s, started_at) "
            "WHERE channel_id = %s AND ended_at IS NULL",
            [now, channel.id],
        )


async def update_title(pool: Pool, session_id: int, title: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE stream_session SET title = %s WHERE id = %s", [title, session_id]
        )


async def next_leg(pool: Pool, channel: Channel, session_id: int, at_least_ms: int = 0) -> Leg:
    """Where the next capture leg starts on the session's audio clock.

    After the previous leg's recorded end, after audio a Transcriber already committed, and
    after `at_least_ms` (the caller's own record of where its last leg ended). Raises
    UnsealedLeg while any leg has no end: how much audio it holds is unknown, so any start
    could overlap its archived tail. A leg a crash left open is sealed first
    (kiroku.backfill.seal_open_legs).
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT coalesce(max(seq), 0) + 1,
                   greatest(
                       coalesce(max(audio_end_ms), 0),
                       (SELECT coalesce(max(audio_end_ms), 0) FROM transcript_span
                        WHERE session_id = %(s)s)),
                   coalesce(bool_or(audio_end_ms IS NULL), false)
            FROM capture_leg WHERE session_id = %(s)s
            """,
            {"s": session_id},
        )
        row = await cur.fetchone()
    assert row is not None
    if row[2]:
        raise UnsealedLeg(f"session {session_id} has a leg with no end; seal it first")
    seq, start_ms = row[0], max(row[1], at_least_ms)
    path = PurePosixPath("audio", channel.login, str(session_id), f"{seq:03d}.opus")
    return Leg(session_id, seq, start_ms, path)


async def record_leg(pool: Pool, leg: Leg, started_at: datetime) -> None:
    """Anchor a leg once its first audio arrives, before that audio reaches a Transcriber
    (utterance.spoken_at is derived from it)."""
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO capture_leg (session_id, seq, audio_start_ms, started_at, archive_path)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (session_id, seq) DO NOTHING
            """,
            [leg.session_id, leg.seq, leg.audio_start_ms, started_at, str(leg.archive_path)],
        )


async def end_leg(
    pool: Pool, leg: Leg, audio_end_ms: int, *, unless_watched: Channel | None = None
) -> bool:
    """Record where a leg ended: by capture when it stops, or when sealing a leg a crash left
    open. The first end recorded stays; later legs were placed after it. Returns whether
    this call recorded it.

    `unless_watched`: record nothing while a `kiroku watch` holds that channel, because the
    leg may be the one it is capturing. Checked in the same statement, so a watch starting
    concurrently either sees the end or isn't capturing yet.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE capture_leg SET audio_end_ms = %(end)s
            WHERE session_id = %(s)s AND seq = %(seq)s AND audio_end_ms IS NULL
              AND NOT (%(channel)s::bigint IS NOT NULL AND EXISTS (
                  SELECT 1 FROM pg_locks
                  WHERE locktype = 'advisory' AND granted AND objsubid = 2
                    AND database = (SELECT oid FROM pg_database
                                    WHERE datname = current_database())
                    AND classid::bigint = %(lock)s AND objid::bigint = %(channel)s))
            """,
            {
                "end": audio_end_ms,
                "s": leg.session_id,
                "seq": leg.seq,
                "channel": unless_watched.id if unless_watched else None,
                "lock": WATCH_LOCK_CLASS,
            },
        )
        return cur.rowcount == 1


async def legs(pool: Pool, session_id: int) -> list[Leg]:
    """The session's recorded legs, in order."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT seq, audio_start_ms, archive_path, audio_end_ms FROM capture_leg "
            "WHERE session_id = %s ORDER BY seq",
            [session_id],
        )
        rows = await cur.fetchall()
    return [Leg(session_id, seq, start, PurePosixPath(path), end) for seq, start, path, end in rows]


async def find_channel(pool: Pool, platform: str, login: str) -> Channel | None:
    """A channel on the watch list, without claiming it (kiroku watch claims channels)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, platform, login FROM channel WHERE platform = %s AND login = %s",
            [platform, login.lower()],
        )
        row = await cur.fetchone()
    return Channel(*row) if row else None


async def find_session(pool: Pool, channel: Channel, session_id: int | None) -> SessionInfo | None:
    """The channel's session with this id, or its latest session when `session_id` is None."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, coalesce(title, '') FROM stream_session
            WHERE channel_id = %s AND (%s::bigint IS NULL OR id = %s)
            ORDER BY started_at DESC, id DESC LIMIT 1
            """,
            [channel.id, session_id, session_id],
        )
        row = await cur.fetchone()
    return SessionInfo(row[0], row[1]) if row else None


def said_until_sql(session_id: sql.SQL, end_ms: sql.SQL) -> sql.Composed:
    """SELECT for the capture leg holding the final sample; also usable in a LATERAL join."""
    return sql.SQL("""
        SELECT started_at + ({end} - audio_start_ms) * interval '1 ms' AS said_until
        FROM capture_leg WHERE session_id = {session} AND audio_start_ms <= {end} - 1
        ORDER BY audio_start_ms DESC, seq DESC LIMIT 1
    """).format(session=session_id, end=end_ms)


async def said_until(pool: Pool, session_id: int, end_ms: int) -> datetime | None:
    """Speech end on the capture leg holding the Window's last audio sample."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            said_until_sql(sql.SQL("%(session)s"), sql.SQL("%(end)s")),
            {"session": session_id, "end": end_ms},
        )
        row = await cur.fetchone()
    return row[0] if row else None


@dataclass(frozen=True, slots=True)
class Session:
    id: int
    channel: str
    title: str
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class Summary(Session):
    category: str | None
    mode: str | None
    alert_count: int
    note_count: int


SUMMARY_SQL = """
    SELECT s.id,c.login,coalesce(seg.title,s.title,''),s.started_at,s.ended_at,
           seg.category,
           (SELECT mode FROM gate_window WHERE session_id=s.id AND mode IS NOT NULL
            ORDER BY start_ms DESC LIMIT 1),
           (SELECT count(*) FROM alert WHERE session_id=s.id),
           (SELECT count(*) FROM note WHERE session_id=s.id)
    FROM stream_session s JOIN channel c ON c.id=s.channel_id
    LEFT JOIN LATERAL (SELECT title,category FROM segment WHERE session_id=s.id
                       ORDER BY start_ms DESC LIMIT 1) seg ON true
"""


async def browse(pool: Pool, *, live: bool | None, limit: int) -> list[Summary]:
    """Session cards, including latest context and counts, in one database round trip."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            SUMMARY_SQL + " WHERE (%s::boolean IS NULL OR (s.ended_at IS NULL)=%s) "
            "ORDER BY s.started_at DESC,s.id DESC LIMIT %s",
            [live, live, limit],
        )
        return [Summary(*row) for row in await cur.fetchall()]


async def summary_by_id(pool: Pool, session_id: int) -> Summary | None:
    async with pool.connection() as conn:
        row = await (await conn.execute(SUMMARY_SQL + " WHERE s.id=%s", [session_id])).fetchone()
        return Summary(*row) if row else None


async def by_id(pool: Pool, session_id: int) -> Session | None:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT s.id,c.login,coalesce(s.title,''),s.started_at,s.ended_at "
                "FROM stream_session s JOIN channel c ON c.id=s.channel_id WHERE s.id=%s",
                [session_id],
            )
        ).fetchone()
    return Session(*row) if row else None
