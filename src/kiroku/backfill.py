"""Backfill: transcribe, from the archive, the audio a crash left out of the transcript.

When `kiroku watch` dies without draining, audio that was archived but still queued for
transcription never gets a transcript_span. It is a gap in coverage. Two steps recover it:

    seal_open_legs()  give each leg a crash left open its archive's length as its end.
                      Capture does this before placing a leg (sessions.next_leg refuses
                      otherwise): an open leg's archived tail would share the next leg's
                      offsets.
    run()             find the session's gaps, decode each from its leg's archive, and feed
                      it through the Transcriber as background audio (it yields to live
                      audio on the same key), committing Results like live ones.

Exactly-once: gaps are computed from committed spans, and a commit of a stored span is a
no-op. Two backfills of one session would still chunk the same gap differently and pay
twice, so run() holds a per-session advisory lock.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from kiroku import archive, ledger, sessions, transcript
from kiroku.archive import ArchiveUnreadable
from kiroku.audio import BYTES_PER_SAMPLE, SAMPLES_PER_MS, AudioBlock, AudioEvent
from kiroku.budget import BudgetGuard
from kiroku.db import Pool
from kiroku.sessions import Channel, Leg
from kiroku.transcriber import Context, Transcriber, TranscriberAuthError

log = logging.getLogger(__name__)

BACKFILL_LOCK_CLASS = 0x6B62  # advisory-lock namespace: one backfill per session
# A gap's audio may decode slightly short of its end: up to one Ogg page (about 1 s) can be
# missing from an archive a crash cut. That much is padded with silence so the gap closes;
# anything shorter in the archive is left as a gap and logged.
MAX_PAD_MS = 1_000

type Span = tuple[int, int]  # (audio_start_ms, audio_end_ms) on the session clock


@dataclass(frozen=True, slots=True)
class Gap:
    leg: Leg  # the leg whose archive holds the audio
    start_ms: int  # session audio clock
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


# ---- pure --------------------------------------------------------------------------------


def sealed_end(
    leg: Leg, next_start: int | None, decoded_ms: int | None, spans: Sequence[Span]
) -> int:
    """Where a leg that a crash left open ended, on the session clock.

    Its archive's decoded length, but never before audio already committed from it (a crash
    can cut the archive's last page after its PCM was transcribed) and never past the next
    leg's start (legs placed before sealing existed may overlap a crashed leg's tail; that
    tail stays unreachable). An unreadable archive ends at the committed audio.
    """
    committed = max(
        (e for s, e in spans if s >= leg.audio_start_ms and (next_start is None or s < next_start)),
        default=leg.audio_start_ms,
    )
    end = committed if decoded_ms is None else max(leg.audio_start_ms + decoded_ms, committed)
    return end if next_start is None else min(end, next_start)


def find_gaps(legs: Sequence[Leg], spans: Sequence[Span]) -> list[Gap]:
    """The parts of each leg that no span covers, in order.

    `legs` in seq order; `spans` in order and non-overlapping, as one Transcriber commits
    them. A leg covers [audio_start_ms, audio_end_ms), cut at the next leg's start. A leg
    with no recorded end is skipped: how much audio it holds is unknown until it is sealed.
    """
    ends = [stop for _, stop in spans]
    gaps: list[Gap] = []
    for i, leg in enumerate(legs):
        if leg.audio_end_ms is None:
            continue
        end = leg.audio_end_ms
        if i + 1 < len(legs):
            end = min(end, legs[i + 1].audio_start_ms)
        cursor = leg.audio_start_ms
        for start, stop in spans[bisect.bisect_right(ends, cursor) :]:
            if start >= end:
                break
            if start > cursor:
                gaps.append(Gap(leg, cursor, start))
            cursor = stop
        if cursor < end:
            gaps.append(Gap(leg, cursor, end))
    return gaps


# ---- shell -------------------------------------------------------------------------------


async def seal_open_legs(
    pool: Pool,
    session_id: int,
    transcriber: str,
    data_dir: Path,
    *,
    unless_watched: Channel | None = None,
) -> None:
    """Record an end for each of the session's legs a crash left open.

    Raises archive.LengthUnknown, leaving the leg open, when its archive can't be read right
    now: sealing it at the committed audio would give up its archived tail for good. An
    archive with nothing decodable (missing, or rejected by ffprobe) ends at the committed
    audio. `unless_watched`: see sessions.end_leg. Idempotent.
    """
    legs = await sessions.legs(pool, session_id)
    if all(leg.audio_end_ms is not None for leg in legs):
        return
    spans = await transcript.spans(pool, session_id, transcriber)
    for i, leg in enumerate(legs):
        if leg.audio_end_ms is not None:
            continue
        next_start = legs[i + 1].audio_start_ms if i + 1 < len(legs) else None
        decoded = await archive.decoded_ms(data_dir / leg.archive_path)
        if decoded is None:
            log.warning(
                "session %d: leg %d archive %s holds no audio",
                session_id,
                leg.seq,
                leg.archive_path,
            )
        end = sealed_end(leg, next_start, decoded, spans)
        if await sessions.end_leg(pool, leg, end, unless_watched=unless_watched):
            log.info("session %d: leg %d was left open; sealed at %d ms", session_id, leg.seq, end)


async def gaps(pool: Pool, session_id: int, transcriber: str) -> list[Gap]:
    legs = await sessions.legs(pool, session_id)
    return find_gaps(legs, await transcript.spans(pool, session_id, transcriber))


async def run(
    pool: Pool,
    session_id: int,
    transcriber: Transcriber,
    data_dir: Path,
    vocabulary: Callable[[], str],
    guard: BudgetGuard,
    *,
    stopping: asyncio.Event | None = None,
    on_commit: Callable[[], None] = lambda: None,
) -> bool:
    """Transcribe and commit the session's gaps. Returns False, doing nothing, while another
    backfill of the session runs.

    Once `stopping` is set, the current gap's audio ends at the next block and what was fed
    is still transcribed and committed. The rest stays a gap for the next run.
    """
    stopping = stopping or asyncio.Event()
    async with _exclusive(pool, session_id) as held:
        if not held:
            log.info("session %d: another backfill is running", session_id)
            return False
        todo = await gaps(pool, session_id, transcriber.name)
        if not todo:
            return True
        total_s = sum(g.duration_ms for g in todo) / 1000
        log.info("session %d: backfilling %d gaps, %.1f s of audio", session_id, len(todo), total_s)
        for gap in todo:
            if stopping.is_set():
                break
            # Each gap is its own Transcriber stream, prompted with the text just before it.
            previous = await transcript.tail_text(
                pool, session_id, transcriber.name, before_ms=gap.start_ms
            )
            audio = _gap_audio(data_dir, gap, stopping)
            context = Context(vocabulary, guard, previous, background=True)
            try:
                async for result in transcriber.transcribe(audio, context):
                    await transcript.commit(pool, session_id, transcriber.name, result)
                    on_commit()
            except TranscriberAuthError as e:
                # A refusal itself bills nothing, but dispatches lost before it may.
                # Record their estimates exactly once, then fail out: the caller logs
                # the run and every transcribed gap stays covered.
                await ledger.record_session_costs(pool, session_id, e.costs)
                raise
            except ArchiveUnreadable as e:
                log.warning("session %d: %s; the gap stays", session_id, e)
        if stopping.is_set():
            log.info("session %d: backfill stopped; the rest resumes next start", session_id)
        else:
            log.info("session %d: backfill done", session_id)
        return True


@asynccontextmanager
async def _exclusive(pool: Pool, session_id: int) -> AsyncGenerator[bool]:
    """Try the session's backfill lock. A transaction-level lock, so it is released however
    the block ends, even if the connection dies; it pins one pool connection meanwhile."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)", [BACKFILL_LOCK_CLASS, session_id]
        )
        row = await cur.fetchone()
        yield bool(row and row[0])


async def _gap_audio(
    data_dir: Path, gap: Gap, stopping: asyncio.Event
) -> AsyncIterator[AudioEvent]:
    """The gap's audio as AudioBlocks on the session clock, as fast as it is consumed."""
    leg_start = gap.leg.audio_start_ms
    first = gap.start_ms * SAMPLES_PER_MS
    want = gap.duration_ms * SAMPLES_PER_MS
    samples = 0
    decoded = archive.decode(
        data_dir / gap.leg.archive_path, gap.start_ms - leg_start, gap.end_ms - leg_start
    )
    async with contextlib.aclosing(decoded):
        async for pcm in decoded:
            yield AudioBlock(first + samples, pcm)
            samples += len(pcm) // BYTES_PER_SAMPLE
            if stopping.is_set():
                return
    short = want - samples
    if 0 < short <= MAX_PAD_MS * SAMPLES_PER_MS:
        yield AudioBlock(first + samples, bytes(short * BYTES_PER_SAMPLE))
    elif short > 0:
        log.warning(
            "leg %d archive holds %d ms less than the gap at %d-%d ms; the rest stays a gap",
            gap.leg.seq,
            short // SAMPLES_PER_MS,
            gap.start_ms,
            gap.end_ms,
        )
