"""Backfill: transcribe, from the archive, the audio a crash left out of the transcript.

When `kiroku watch` dies without draining, audio that was archived but still queued for
transcription never gets a transcript_span. It is a gap in coverage. On the next start:

    prepare()   seal the legs a crash left open at their archive's length, then find gaps
    run()       decode each gap from its leg's archive and feed it through the Transcriber,
                committing Results like live ones, paced so it stays behind live audio

Sealing comes first because sessions.next_leg places the next leg after the recorded leg
ends; an open leg's archived tail would otherwise share offsets with it.

Exactly-once needs no extra machinery: gaps are computed from committed spans, a commit
is a no-op for a span already stored, and one process per channel (the advisory lock in
sessions.claim_channels) runs at most one backfill per session.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from kiroku import archive, sessions, transcript
from kiroku.archive import ArchiveUnreadable
from kiroku.audio import BYTES_PER_SAMPLE, SAMPLE_RATE, SAMPLES_PER_MS, AudioBlock, AudioEvent
from kiroku.db import Pool
from kiroku.sessions import Leg
from kiroku.transcriber import Context, Result, Transcriber

log = logging.getLogger(__name__)

# Backfill audio is fed at most this many times real time. Live audio arrives at 1x, so
# backfill's share of the Transcriber's per-key rate limits stays at most twice live's (Groq
# free tier: 20 requests a minute; live makes 3-4). A 30-minute backlog clears in 15 min.
BACKFILL_PACE = 2.0
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
    """The parts of each leg that no span covers, in order. `legs` are in seq order.

    A leg covers [audio_start_ms, audio_end_ms), cut at the next leg's start. A leg with no
    recorded end is skipped: how much audio it holds is unknown until it is sealed.
    """
    ordered = sorted(spans)
    gaps: list[Gap] = []
    for i, leg in enumerate(legs):
        if leg.audio_end_ms is None:
            continue
        end = leg.audio_end_ms
        if i + 1 < len(legs):
            end = min(end, legs[i + 1].audio_start_ms)
        cursor = leg.audio_start_ms
        for start, stop in ordered:
            if start >= end:
                break
            if stop <= cursor:
                continue
            if start > cursor:
                gaps.append(Gap(leg, cursor, start))
            cursor = stop
        if cursor < end:
            gaps.append(Gap(leg, cursor, end))
    return gaps


# ---- shell -------------------------------------------------------------------------------


async def prepare(pool: Pool, session_id: int, transcriber: str, data_dir: Path) -> list[Gap]:
    """Seal the session's legs a crash left open, then return its gaps for `transcriber`.

    Must finish before the session's next leg is placed. Idempotent: a sealed end never
    moves (sessions.end_leg), and a gap disappears once its spans are committed.
    """
    legs = await sessions.legs(pool, session_id)
    spans = await transcript.spans(pool, session_id, transcriber)
    for i, leg in enumerate(legs):
        if leg.audio_end_ms is not None:
            continue
        next_start = legs[i + 1].audio_start_ms if i + 1 < len(legs) else None
        decoded = await archive.decoded_ms(data_dir / leg.archive_path)
        end = sealed_end(leg, next_start, decoded, spans)
        if decoded is None:
            log.warning(
                "session %d: leg %d archive %s is unreadable", session_id, leg.seq, leg.archive_path
            )
        log.info("session %d: leg %d was left open; sealed at %d ms", session_id, leg.seq, end)
        await sessions.end_leg(pool, leg, end)
        legs[i] = dataclasses.replace(leg, audio_end_ms=end)
    return find_gaps(legs, spans)


async def run(
    pool: Pool,
    session_id: int,
    transcriber: Transcriber,
    data_dir: Path,
    gaps: Sequence[Gap],
    vocabulary: Callable[[], str],
    *,
    commit: Callable[[Result], Awaitable[object]],
    pace: float | None = BACKFILL_PACE,
    stopping: asyncio.Event | None = None,
) -> None:
    """Transcribe each gap and hand its Results to `commit`. `pace=None` doesn't throttle.

    Once `stopping` is set, the current gap's audio ends at the next block and what was fed
    is still transcribed and committed. The rest stays a gap for the next prepare().
    """
    stopping = stopping or asyncio.Event()
    total_s = sum(g.duration_ms for g in gaps) / 1000
    log.info("session %d: backfilling %d gaps, %.1f s of audio", session_id, len(gaps), total_s)
    for gap in gaps:
        if stopping.is_set():
            break
        # Each gap is its own Transcriber stream, prompted with the text just before it.
        previous = await transcript.tail_text(
            pool, session_id, transcriber.name, before_ms=gap.start_ms
        )
        audio = _gap_audio(data_dir, gap, pace, stopping)
        try:
            async for result in transcriber.transcribe(audio, Context(vocabulary, previous)):
                await commit(result)
        except ArchiveUnreadable as e:
            log.warning("session %d: %s; the gap stays", session_id, e)
    if stopping.is_set():
        log.info("session %d: backfill stopped; the rest resumes on the next start", session_id)
    else:
        log.info("session %d: backfill done", session_id)


async def _gap_audio(
    data_dir: Path, gap: Gap, pace: float | None, stopping: asyncio.Event
) -> AsyncIterator[AudioEvent]:
    """The gap's audio as AudioBlocks on the session clock, at most `pace` x real time."""
    leg_start = gap.leg.audio_start_ms
    first = gap.start_ms * SAMPLES_PER_MS
    want = gap.duration_ms * SAMPLES_PER_MS
    samples = 0
    loop = asyncio.get_running_loop()
    began = loop.time()
    decoded = archive.decode(
        data_dir / gap.leg.archive_path, gap.start_ms - leg_start, gap.end_ms - leg_start
    )
    async with contextlib.aclosing(decoded):
        async for pcm in decoded:
            yield AudioBlock(first + samples, pcm)
            samples += len(pcm) // BYTES_PER_SAMPLE
            if stopping.is_set():
                return
            if pace is not None:
                ahead = samples / SAMPLE_RATE / pace - (loop.time() - began)
                if ahead > 0:
                    await asyncio.sleep(ahead)
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
