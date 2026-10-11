"""Deterministic speech-like PCM: tone bursts with pauses, optionally over a noise bed.

Real stream audio can't live in the repo, so tests synthesize audio whose pauses are known.
"""

from __future__ import annotations

import io
import math
import random
import wave
from array import array

RATE = 16_000


def speechlike(
    seconds: float, *, seed: int = 0, noise_db: float | None = None
) -> tuple[bytes, list[tuple[int, int]]]:
    """PCM (s16le mono 16 kHz) and the pauses in it, as (start_ms, end_ms) pairs.

    Bursts of 1-4 s of amplitude-modulated tone, separated by 0.4-1.0 s pauses. With
    `noise_db`, a constant noise bed at that level (dBFS) runs under everything.
    """
    rng = random.Random(seed)
    total = int(seconds * RATE)
    out = array("h", bytes(total * 2))
    pauses: list[tuple[int, int]] = []
    t = 0
    while t < total:
        burst = int(rng.uniform(1.0, 4.0) * RATE)
        freq = rng.uniform(150, 400)
        for i in range(t, min(t + burst, total)):
            envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 4 * i / RATE)
            out[i] = int(8000 * envelope * math.sin(2 * math.pi * freq * i / RATE))
        t += burst
        gap = int(rng.uniform(0.4, 1.0) * RATE)
        if t < total:
            pauses.append((t * 1000 // RATE, min(t + gap, total) * 1000 // RATE))
        t += gap
    if noise_db is not None:
        amp = 32767 * 10 ** (noise_db / 20)
        for i in range(total):
            out[i] = max(-32768, min(32767, out[i] + int(rng.gauss(0, amp))))
    return out.tobytes(), pauses


def tone(seconds: float, freq: float = 300.0) -> bytes:
    """Continuous tone: no pauses at all."""
    n = int(seconds * RATE)
    return array(
        "h", (int(6000 * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n))
    ).tobytes()


def wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue()
