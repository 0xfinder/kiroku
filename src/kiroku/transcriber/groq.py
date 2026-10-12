"""Groq whisper-large-v3-turbo behind the streaming Transcriber interface (Fallback, D3/D20).

Inside, per session: audio events -> SilenceChunker -> queue -> one sequential worker that
calls Groq and yields Results. Calls are sequential because each prompt carries the previous
chunk's text. While Groq rate-limits, the queue grows rather than dropping audio, up to
`max_backlog_ms`; past that the oldest chunk is skipped (logged, and visible as a missing
span). One GroqTranscriber serves every session, and a 429 on any session pauses all of
them, because the limit is per API key.

Background streams (Backfill, `Context.background`) use the key's spare capacity only: a
background call waits until no live chunk on the key is queued or in flight and the key is
not paused, and background calls start at most once per `background_interval_s` across all
sessions. Their audio comes from the archive, so instead of a backlog they get backpressure:
the producer reads no further ahead than the next chunk, and nothing is skipped.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any, cast

import httpx

from kiroku.audio import BYTES_PER_SAMPLE, SAMPLE_RATE, AudioBlock, AudioEvent, ms
from kiroku.config import Secret
from kiroku.errors import KirokuError
from kiroku.ledger import AudioSeconds, CostEntry, Purpose
from kiroku.transcriber import Context, Result, Utterance
from kiroku.transcriber.chunker import Chunk, ChunkPolicy, SilenceChunker

log = logging.getLogger(__name__)

PROVIDER = "groq"
MODEL = "whisper-large-v3-turbo"
URL = "https://api.groq.com/openai/v1/audio/transcriptions"
USD_PER_AUDIO_HOUR = Decimal("0.04")
MIN_BILLED_SECONDS = Decimal(10)
PROMPT_TAIL_CHARS = 400  # Whisper reads at most 224 prompt tokens
# Background calls start at most this often per key: 6 requests a minute with 15-25 s chunks,
# about 2x real time. Groq's free tier allows 20 a minute; live uses 3-4 per channel.
BACKGROUND_INTERVAL_S = 10.0
NO_SPEECH_DROP = 0.6  # Whisper's own heuristic: drop a segment above this no_speech_prob
LOGPROB_DROP = -1.0  # ...when its avg_logprob is also below this


class GroqAuthError(KirokuError):
    """401/403: the key is wrong. Stops `kiroku watch` instead of skipping every chunk."""


class GroqTranscriber:
    def __init__(
        self,
        http: httpx.AsyncClient,
        key: Secret,
        *,
        policy: ChunkPolicy | None = None,
        max_backlog_ms: int = 30 * 60_000,
        background_interval_s: float = BACKGROUND_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._key = key
        self._policy = policy or ChunkPolicy()
        self._max_backlog_ms = max_backlog_ms
        self._background_interval_s = background_interval_s
        self._sleep = sleep
        # Monotonic time before which no session may call Groq. Writers only raise it.
        self._not_before = 0.0
        # Voiced live chunks queued or in flight, over every session; set while zero.
        self._live_pending = 0
        self._live_idle = asyncio.Event()
        self._live_idle.set()
        # Background calls take turns, and a turn starts no sooner than this.
        self._background_turn = asyncio.Lock()
        self._background_after = 0.0

    @property
    def name(self) -> str:
        return f"{PROVIDER}:{MODEL}"

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
                # Live only in effect: a background stream's backpressure keeps this at 0.
                if backlog_ms > self._max_backlog_ms:
                    log.warning(
                        "Groq backlog over %d ms; skipping chunk at %d ms",
                        self._max_backlog_ms,
                        ms(chunk.start_sample),
                    )
                    done_live(chunk)
                    continue
                if not chunk.voiced:
                    yield Result(ms(chunk.start_sample), ms(chunk.end_sample), (), None)
                    continue
                prompt = build_prompt(context.vocabulary(), tail)
                try:
                    result, text = await self._call(chunk, prompt, background)
                finally:
                    done_live(chunk)
                tail = (tail + " " + text).strip() if text else tail
                yield result
        finally:
            producer.cancel()
            self._add_live(-live_pending)  # chunks this stream will never send
        await producer  # re-raise a failure in the audio source

    def _add_live(self, n: int) -> None:
        self._live_pending += n
        if self._live_pending:
            self._live_idle.clear()
        else:
            self._live_idle.set()

    async def _turn(self, background: bool) -> None:
        """Wait until a call may be sent. Live waits only for a rate-limit pause."""
        if not background:
            delay = self._not_before - time.monotonic()
            if delay > 0:
                await self._sleep(delay)
            return
        async with self._background_turn:
            while True:
                await self._live_idle.wait()
                delay = max(self._not_before, self._background_after) - time.monotonic()
                if delay > 0:
                    await self._sleep(delay)
                if self._live_idle.is_set():
                    break
            self._background_after = time.monotonic() + self._background_interval_s

    async def _call(self, chunk: Chunk, prompt: str, background: bool) -> tuple[Result, str]:
        start_ms, end_ms = ms(chunk.start_sample), ms(chunk.end_sample)
        attempt = 0
        maybe_billed = 0  # sent, but the response was lost: Groq may have billed it
        while True:
            await self._turn(background)
            started = time.monotonic()
            try:
                response = await self._http.post(
                    URL,
                    headers={"Authorization": f"Bearer {self._key.reveal()}"},
                    files={"file": ("chunk.wav", wav_bytes(chunk.pcm), "audio/wav")},
                    data={
                        "model": MODEL,
                        "language": "en",
                        "prompt": prompt,
                        "response_format": "verbose_json",
                        "temperature": "0",
                    },
                )
            except httpx.TransportError as e:
                attempt += 1
                if not isinstance(e, httpx.ConnectError | httpx.ConnectTimeout):
                    maybe_billed += 1
                log.warning("Groq request failed (%s); retrying", e)
                await self._sleep(retry_delay(None, {}, attempt))
                continue
            latency_ms = int((time.monotonic() - started) * 1000)
            status = response.status_code
            if status in (401, 403):
                raise GroqAuthError(f"Groq rejected the API key (HTTP {status})")
            if status == 429 or status >= 500:
                attempt += 1
                wait = retry_delay(status, response.headers, attempt)
                if status == 429:
                    self._not_before = max(self._not_before, time.monotonic() + wait)
                    log.warning("Groq rate limit; pausing all sessions for %.1f s", wait)
                else:
                    await self._sleep(wait)
                continue
            if status >= 400:
                # Bad audio or a malformed request: retrying cannot help, and nothing was
                # billed. Commit an empty span so the session moves on.
                log.error("Groq HTTP %d for chunk at %d ms: %s", status, start_ms, response.text)
                return Result(start_ms, end_ms, (), None), ""
            utterances, text = to_utterances(start_ms, end_ms, response.json())
            seconds, usd = billed(end_ms - start_ms)
            # Over-report rather than under-report: Budget caps trust the ledger.
            seconds, usd = seconds * (1 + maybe_billed), usd * (1 + maybe_billed)
            cost = CostEntry(
                PROVIDER, MODEL, Purpose.TRANSCRIBE, AudioSeconds(seconds), usd, latency_ms
            )
            return Result(start_ms, end_ms, utterances, cost), text


def _duration_ms(chunk: Chunk) -> int:
    return ms(chunk.end_sample) - ms(chunk.start_sample)


def wav_bytes(pcm: bytes) -> bytes:
    """Wrap PCM in a WAV header. Uploading WAV costs no encode CPU (Opus would)."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return out.getvalue()


def build_prompt(vocabulary: str, tail: str) -> str:
    """Vocabulary first, then the end of the previous text, cut at a word boundary."""
    if len(tail) > PROMPT_TAIL_CHARS:
        tail = tail[-PROMPT_TAIL_CHARS:].split(" ", 1)[-1]
    return f"{vocabulary} {tail}".strip()


def billed(duration_ms: int) -> tuple[Decimal, Decimal]:
    """Groq bills max(duration, 10 s) at $0.04 per audio hour. Returns (seconds, usd)."""
    seconds = max(Decimal(duration_ms) / 1000, MIN_BILLED_SECONDS)
    return seconds, (seconds * USD_PER_AUDIO_HOUR / 3600).quantize(Decimal("0.000001"))


_GO_DURATION = re.compile(
    r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?"
)


def _go_seconds(text: str) -> float | None:
    """'2m59.56s' -> 179.56, '750ms' -> 0.75. Groq's reset headers use Go durations."""
    match = _GO_DURATION.fullmatch(text.strip())
    if not match or not any(match.groups()):
        return None
    h, m, s, milli = (float(g) if g else 0.0 for g in match.groups())
    return h * 3600 + m * 60 + s + milli / 1000


def retry_delay(status: int | None, headers: Mapping[str, str], attempt: int) -> float:
    """Seconds to wait before retrying the same chunk.

    429: `retry-after` if present, else the larger of the audio-seconds and request reset
    headers. Otherwise (5xx, network): 1, 2, 4 ... seconds, capped at 60.
    """
    backoff = float(min(2 ** (attempt - 1), 60))
    if status != 429:
        return backoff
    if (value := headers.get("retry-after")) is not None:
        try:
            return max(float(value), 1.0)
        except ValueError:
            pass
    resets = [
        _go_seconds(headers[h])
        for h in ("x-ratelimit-reset-audio-seconds", "x-ratelimit-reset-requests")
        if h in headers
    ]
    waits = [r for r in resets if r is not None]
    return max(waits) if waits else backoff


def to_utterances(
    start_ms: int, end_ms: int, body: Mapping[str, Any]
) -> tuple[tuple[Utterance, ...], str]:
    """verbose_json -> Utterances on the session clock, and the chunk's kept text.

    Segment times are clamped to the chunk and starts made strictly increasing, because
    utterance is unique on its start. Segments Whisper itself flags as likely non-speech
    are dropped. A body without `segments` is a contract break and raises.
    """
    segments = cast(list[Mapping[str, Any]], body["segments"])
    out: list[Utterance] = []
    last_start = start_ms - 1
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        if (
            float(seg.get("no_speech_prob", 0.0)) > NO_SPEECH_DROP
            and float(seg.get("avg_logprob", 0.0)) < LOGPROB_DROP
        ):
            continue
        s = min(max(start_ms + round(float(seg["start"]) * 1000), last_start + 1), end_ms - 1)
        e = min(max(start_ms + round(float(seg["end"]) * 1000), s + 1), end_ms)
        if s <= last_start and out:
            # No room left for a distinct start: merge into the previous utterance.
            prev = out[-1]
            out[-1] = Utterance(prev.start_ms, max(prev.end_ms, e), f"{prev.text} {text}")
            continue
        out.append(Utterance(s, e, text))
        last_start = s
    return tuple(out), " ".join(u.text for u in out)
