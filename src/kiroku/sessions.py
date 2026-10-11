"""Channels, Stream sessions and capture legs: the only writer of `channel`,
`stream_session` and `capture_leg`.

A session is one broadcast (the platform's stream id). Opening converges whatever state a
crash, a restart or a Twitch blip left behind: the same broadcast always maps to the same
session, and a new broadcast closes the stale one first.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from kiroku.db import Pool, connect
from kiroku.errors import KirokuError
from kiroku.source import StreamMeta

WATCH_LOCK_CLASS = 0x6B72  # advisory-lock namespace: one `kiroku watch` per channel


class ChannelBusy(KirokuError):
    pass


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


@asynccontextmanager
async def claim_channels(
    database_url: str, platform: str, logins: Sequence[str]
) -> AsyncGenerator[list[Channel]]:
    """Upsert the channels and hold an advisory lock on each for the life of the block.

    Two watchers on one channel would write the same session twice. The locks live on a
    dedicated connection (session locks belong to a connection), released when it closes.
    """
    async with connect(database_url) as conn:
        channels: list[Channel] = []
        for login in logins:
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


async def next_leg(pool: Pool, channel: Channel, session_id: int) -> Leg:
    """Where the next capture leg starts on the session's audio clock.

    After the previous leg's recorded end, and never before audio a Transcriber already
    committed. After a crash the previous leg's end is unknown; its untranscribed tail
    then becomes unreachable rather than overlapping the next leg's offsets.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT coalesce(max(seq), 0) + 1,
                   greatest(
                       coalesce(max(coalesce(audio_end_ms, audio_start_ms)), 0),
                       (SELECT coalesce(max(audio_end_ms), 0) FROM transcript_span
                        WHERE session_id = %(s)s))
            FROM capture_leg WHERE session_id = %(s)s
            """,
            {"s": session_id},
        )
        row = await cur.fetchone()
    assert row is not None
    seq, start_ms = row
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


async def end_leg(pool: Pool, leg: Leg, audio_end_ms: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE capture_leg SET audio_end_ms = %s WHERE session_id = %s AND seq = %s",
            [audio_end_ms, leg.session_id, leg.seq],
        )
