"""Bounded live PCM and lossless Backfill admission shared by chunked Transcribers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from kiroku.audio import AudioBlock, AudioEvent, ms
from kiroku.transcriber import Context, Result
from kiroku.transcriber.chunker import Chunk, ChunkPolicy, SilenceChunker

log = logging.getLogger(__name__)


class ChunkedTranscriber(ABC):
    def __init__(self, policy: ChunkPolicy | None, max_backlog_ms: int) -> None:
        self._policy = policy or ChunkPolicy()
        self._max_backlog_ms = max_backlog_ms
        self._live_pending = 0
        self._live_idle = asyncio.Event()
        self._live_idle.set()

    @abstractmethod
    async def _transcribe_chunk(self, chunk: Chunk, context: Context, tail: str) -> Result | None:
        """None leaves an unfinalized gap, for example after Budget refusal."""
        ...

    async def transcribe(
        self, audio: AsyncIterator[AudioEvent], context: Context
    ) -> AsyncIterator[Result]:
        background = context.background
        chunks: asyncio.Queue[Chunk | None] = asyncio.Queue()
        room = asyncio.Semaphore(1) if background else None  # backpressure, background only
        backlog_ms = 0
        live_pending = 0  # this stream's share of self._live_pending

        async def enqueue(ready: list[Chunk]) -> None:
            nonlocal backlog_ms, live_pending
            for chunk in ready:
                if room is not None:
                    await room.acquire()
                elif chunk.voiced:
                    live_pending += 1
                    self._add_live(1)
                backlog_ms += _duration_ms(chunk)
                chunks.put_nowait(chunk)
                # Bound retained PCM even while the consumer is sleeping on a 429.
                # Keep the newest chunk if it alone exceeds the configured tail.
                while room is None and backlog_ms > self._max_backlog_ms and chunks.qsize() > 1:
                    skipped = chunks.get_nowait()
                    assert skipped is not None  # only produce's finally enqueues the sentinel
                    backlog_ms -= _duration_ms(skipped)
                    done_live(skipped)
                    log.warning(
                        "Transcription backlog over %d ms; skipping chunk at %d ms",
                        self._max_backlog_ms,
                        ms(skipped.start_sample),
                    )

        def done_live(chunk: Chunk) -> None:
            nonlocal live_pending
            if not background and chunk.voiced:
                live_pending -= 1
                self._add_live(-1)

        async def produce() -> None:
            try:
                chunker: SilenceChunker | None = None
                async for event in audio:
                    if isinstance(event, AudioBlock):
                        chunker = chunker or SilenceChunker(event.start_sample, self._policy)
                        await enqueue(chunker.feed(event.pcm))
                    else:
                        await enqueue(chunker.flush() if chunker else [])
                        chunker = None
                if chunker:
                    await enqueue(chunker.flush())
            finally:
                # Always end the consumer, so a failure here surfaces at `await producer`
                # instead of leaving the consumer waiting forever.
                chunks.put_nowait(None)

        producer = asyncio.create_task(produce())
        tail = context.previous_text
        try:
            while (chunk := await chunks.get()) is not None:
                if room is not None:
                    room.release()
                backlog_ms -= _duration_ms(chunk)
                if not chunk.voiced:
                    yield Result(ms(chunk.start_sample), ms(chunk.end_sample), (), None)
                    continue
                try:
                    result = await self._transcribe_chunk(chunk, context, tail)
                finally:
                    done_live(chunk)
                if result is None:
                    continue
                text = " ".join(u.text for u in result.utterances)
                tail = (tail + " " + text).strip() if text else tail
                yield result
        finally:
            producer.cancel()
            self._add_live(-live_pending)  # chunks this stream will never send
            await asyncio.gather(producer, return_exceptions=True)
        await producer  # re-raise a failure in the audio source

    def _add_live(self, n: int) -> None:
        self._live_pending += n
        if self._live_pending:
            self._live_idle.clear()
        else:
            self._live_idle.set()


def _duration_ms(chunk: Chunk) -> int:
    return ms(chunk.end_sample) - ms(chunk.start_sample)
