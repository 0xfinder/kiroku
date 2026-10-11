"""Silence chunking for request/response Transcribers. Pure: no I/O, no clock.

Speech is cut at pauses, so words and sentences aren't split (spike finding 6). The pause
threshold adapts to the recent audio, because game audio and music under the voice mean a
stream is rarely truly silent: over the last 30 s of 20 ms frame levels, `lo` is the 3rd
percentile and `hi` the 90th, and a frame is quiet below lo + 25% of the range.
Measured cost: about 0.2% of one CPU core per live channel.
"""

from __future__ import annotations

import math
from array import array
from collections import deque
from dataclasses import dataclass

from kiroku.audio import BYTES_PER_SAMPLE, SAMPLES_PER_MS

FRAME_MS = 20
FRAME_SAMPLES = FRAME_MS * SAMPLES_PER_MS
FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE
CONTEXT_FRAMES = 30_000 // FRAME_MS  # 30 s of levels for the adaptive threshold
RECOMPUTE_EVERY = 25  # frames (0.5 s) between threshold updates
MIN_RANGE_DB = 6.0  # flat audio has no pauses to find


@dataclass(frozen=True, slots=True)
class ChunkPolicy:
    min_ms: int = 15_000  # no pause cut before this; Groq bills 10 s minimum per request
    max_ms: int = 25_000  # forced cut; bounds speech-to-transcript latency
    pause_ms: int = 300  # a pause this long licenses a cut, at its middle
    forced_window_ms: int = 5_000  # a forced cut lands on the quietest 100 ms in this tail
    voiced_ms: int = 300  # a chunk with less audible audio than this is silence


@dataclass(frozen=True, slots=True)
class Chunk:
    start_sample: int
    pcm: bytes
    voiced: bool  # False: send nothing, just record the span

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.pcm) // BYTES_PER_SAMPLE


def frame_level_db(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame)
    power = sum(x * x for x in samples) / max(len(samples), 1)
    return 10 * math.log10(power + 1)


class SilenceChunker:
    """Feed contiguous PCM; get back Chunks that tile it exactly, in order."""

    def __init__(self, start_sample: int, policy: ChunkPolicy | None = None) -> None:
        self._policy = policy or ChunkPolicy()
        self._start = start_sample  # first sample of the current chunk
        self._buf = bytearray()  # PCM of the current chunk
        self._partial = b""  # bytes of an incomplete frame
        self._levels: list[float] = []  # frame levels of the current chunk
        self._context: deque[float] = deque(maxlen=CONTEXT_FRAMES)
        self._threshold = -math.inf
        self._spread = 0.0
        self._since_recompute = 0

    def feed(self, pcm: bytes) -> list[Chunk]:
        out: list[Chunk] = []
        data = self._partial + pcm
        whole = len(data) - len(data) % FRAME_BYTES
        self._partial = data[whole:]
        for i in range(0, whole, FRAME_BYTES):
            frame = data[i : i + FRAME_BYTES]
            self._buf += frame
            level = frame_level_db(frame)
            self._levels.append(level)
            self._context.append(level)
            self._update_threshold()
            cut = self._cut_frame()
            if cut is not None:
                out.append(self._take(cut))
        return out

    def flush(self) -> list[Chunk]:
        """Cut everything buffered, including a trailing partial frame."""
        self._buf += self._partial
        self._partial = b""
        if not self._buf:
            return []
        return [self._take(len(self._levels) + (1 if len(self._buf) % FRAME_BYTES else 0))]

    def _update_threshold(self) -> None:
        self._since_recompute += 1
        if self._since_recompute < RECOMPUTE_EVERY and self._threshold > -math.inf:
            return
        self._since_recompute = 0
        ordered = sorted(self._context)
        lo = ordered[len(ordered) * 3 // 100]
        hi = ordered[len(ordered) * 9 // 10]
        self._spread = hi - lo
        self._threshold = lo + 0.25 * (hi - lo)

    def _cut_frame(self) -> int | None:
        """Index of the frame to cut before, or None to keep going."""
        p = self._policy
        n = len(self._levels)
        if n * FRAME_MS < p.min_ms:
            return None
        pause_frames = p.pause_ms // FRAME_MS
        if self._spread >= MIN_RANGE_DB and all(
            level <= self._threshold for level in self._levels[-pause_frames:]
        ):
            return n - pause_frames // 2
        if n * FRAME_MS >= p.max_ms:
            window = p.forced_window_ms // FRAME_MS
            span = 100 // FRAME_MS
            first = n - window
            best = min(range(first, n - span + 1), key=lambda j: sum(self._levels[j : j + span]))
            return best + span // 2
        return None

    def _take(self, frames: int) -> Chunk:
        """Emit the first `frames` frames as a Chunk and keep the rest buffered."""
        cut_bytes = min(frames * FRAME_BYTES, len(self._buf))
        audible = sum(1 for level in self._levels[:frames] if level > self._threshold)
        chunk = Chunk(
            self._start,
            bytes(self._buf[:cut_bytes]),
            voiced=audible * FRAME_MS >= self._policy.voiced_ms,
        )
        del self._buf[:cut_bytes]
        del self._levels[:frames]
        self._start = chunk.end_sample
        return chunk
