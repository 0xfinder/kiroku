"""The PCM format every stage agrees on, and the audio events a Transcriber consumes.

Positions are on the session's audio clock: samples (16 per ms) since the session's first
captured audio. The clock only moves forward and has no gap across reconnects; wall-clock
gaps live in capture_leg.started_at.
"""

from __future__ import annotations

from dataclasses import dataclass

SAMPLE_RATE = 16_000
SAMPLES_PER_MS = SAMPLE_RATE // 1000
BYTES_PER_SAMPLE = 2  # s16le mono

# ffmpeg output options for the PCM every stage reads.
FFMPEG_PCM = ["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE)]


@dataclass(frozen=True, slots=True)
class AudioBlock:
    start_sample: int  # session audio clock
    pcm: bytes  # s16le mono 16 kHz, whole samples

    @property
    def end_sample(self) -> int:
        return self.start_sample + len(self.pcm) // BYTES_PER_SAMPLE


@dataclass(frozen=True, slots=True)
class LegEnded:
    """A capture leg ended (stream drop or stop). Speech never spans a reconnect."""


type AudioEvent = AudioBlock | LegEnded


def ms(sample: int) -> int:
    return sample // SAMPLES_PER_MS
