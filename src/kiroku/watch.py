"""`kiroku watch`: the per-channel lifecycle and the per-session audio pipeline.

    watch()                           one task per channel
      _watch_channel                  probe every poll_s -> step() -> actions
        SessionRun (while Live)
          _capture   Source legs -> capture_leg rows -> AudioBlocks -> queue  (reconnects)
          _transcribe  queue -> Transcriber -> Results -> transcript.commit     (drains)
          _gating    GateLoop: committed transcript -> Windows -> Gate -> gating.commit

The lifecycle is a pure state machine (`step`); the shell executes its actions. Every
action is idempotent in the database (kiroku.sessions), so the shell never needs to know
whether it is starting fresh, resuming after a crash, or recovering from a Twitch blip.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from kiroku import sessions, transcript
from kiroku.audio import BYTES_PER_SAMPLE, SAMPLES_PER_MS, AudioBlock, AudioEvent, LegEnded
from kiroku.budget import Budget
from kiroku.db import Pool
from kiroku.gate import Gate
from kiroku.gate.windows import JSON
from kiroku.gating import GateLoop
from kiroku.sessions import Channel
from kiroku.source import Capture, NotLive, Online, Probe, ProbeFailed, Source, StreamMeta
from kiroku.transcriber import Context, Result, Transcriber

log = logging.getLogger(__name__)

OFFLINE_PROBES_TO_CLOSE = 2  # one NotLive blip doesn't stop capture
CAPTURE_STOP_TIMEOUT_S = 15.0
DRAIN_TIMEOUT_S = 120.0
GATE_DRAIN_TIMEOUT_S = 60.0

# ---- lifecycle: pure ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Unknown:
    """Just started. The database may hold an open session from a previous run."""


@dataclass(frozen=True, slots=True)
class Offline:
    pass


@dataclass(frozen=True, slots=True)
class Live:
    meta: StreamMeta
    offline_probes: int = 0


type ChannelState = Unknown | Offline | Live


@dataclass(frozen=True, slots=True)
class Open:
    meta: StreamMeta


@dataclass(frozen=True, slots=True)
class Update:
    meta: StreamMeta


@dataclass(frozen=True, slots=True)
class Close:
    pass


type Action = Open | Update | Close


def step(state: ChannelState, probe: Probe) -> tuple[ChannelState, tuple[Action, ...]]:
    """The whole channel lifecycle.

    | state       | probe               | next        | actions          |
    |-------------|---------------------|-------------|------------------|
    | any         | ProbeFailed         | same        | ()               |
    | Unknown     | NotLive             | Offline     | (Close,) stale   |
    | Offline     | NotLive             | Offline     | ()               |
    | Live(n)     | NotLive, n+1 < 2    | Live(n+1)   | ()               |
    | Live(n)     | NotLive, n+1 >= 2   | Offline     | (Close,)         |
    | Unknown/Off | Online(m)           | Live(m)     | (Open(m),)       |
    | Live(a)     | Online(m), same id  | Live(m)     | (Update(m),) if changed |
    | Live(a)     | Online(m), new id   | Live(m)     | (Close, Open(m)) |
    """
    match state, probe:
        case _, ProbeFailed():
            return state, ()
        case Unknown(), NotLive():
            return Offline(), (Close(),)
        case Offline(), NotLive():
            return Offline(), ()
        case Live(meta, n), NotLive():
            if n + 1 < OFFLINE_PROBES_TO_CLOSE:
                return Live(meta, n + 1), ()
            return Offline(), (Close(),)
        case Unknown() | Offline(), Online(meta):
            return Live(meta), (Open(meta),)
        case Live(old), Online(meta) if old.stream_id == meta.stream_id:
            return Live(meta), ((Update(meta),) if meta != old else ())
        case Live(), Online(meta):
            return Live(meta), (Close(), Open(meta))
    raise AssertionError(f"unhandled {state!r} {probe!r}")


# ---- shell -------------------------------------------------------------------------------


async def watch(
    pool: Pool,
    channels: Sequence[tuple[Channel, Source]],
    transcriber: Transcriber,
    data_dir: Path,
    *,
    budget: Budget,
    gate: Gate | None,
    poll_s: float = 60.0,
) -> None:
    """Run until cancelled or a session fails with an error that needs a human. With no
    `gate`, sessions are transcribed but not gated."""
    services = Services(pool, transcriber, data_dir, budget, gate)
    async with asyncio.TaskGroup() as tg:
        for channel, source in channels:
            tg.create_task(_watch_channel(services, channel, source, poll_s))


@dataclass(frozen=True, slots=True)
class Services:
    """What every session of this process shares."""

    pool: Pool
    transcriber: Transcriber
    data_dir: Path
    budget: Budget
    gate: Gate | None


async def _watch_channel(
    services: Services, channel: Channel, source: Source, poll_s: float
) -> None:
    pool = services.pool
    state: ChannelState = Unknown()
    run: SessionRun | None = None
    try:
        while True:
            if run is not None:
                run.raise_if_failed()
            probe = await source.probe()
            if isinstance(probe, ProbeFailed):
                log.warning("%s: probe failed: %s", channel.login, probe.reason)
            next_state, actions = step(state, probe)
            try:
                for action in actions:
                    match action:
                        case Close():
                            if run is not None:
                                await run.stop(session_ended=True)
                                run = None
                            await sessions.close_open_sessions(pool, channel, datetime.now(UTC))
                        case Open(meta):
                            session_id = await sessions.open_session(
                                pool, channel, meta, datetime.now(UTC)
                            )
                            log.info("%s: live, session %d", channel.login, session_id)
                            run = SessionRun(services, channel, source, session_id, meta)
                            await run.start()
                        case Update(meta):
                            if run is not None:
                                await sessions.update_title(pool, run.session_id, meta.title)
                                run.meta = meta
                # Advance only once the actions are done. They are idempotent, so after a
                # database blip the next probe retries the same transition.
                state = next_state
            except psycopg.OperationalError as e:
                log.warning("%s: database error (%s); retrying next poll", channel.login, e)
            await asyncio.sleep(poll_s)
    finally:
        if run is not None:
            # Shutdown: drain, but leave the session open. A restart resumes it.
            await run.stop()


class SessionRun:
    """One live session in this process: capture legs and transcription."""

    def __init__(
        self,
        services: Services,
        channel: Channel,
        source: Source,
        session_id: int,
        meta: StreamMeta,
        *,
        reconnect_max_s: float = 30.0,
    ) -> None:
        self.session_id = session_id
        self.meta = meta
        self._pool = services.pool
        self._channel = channel
        self._source = source
        self._transcriber = services.transcriber
        self._data_dir = services.data_dir
        self._guard = services.budget.guard(session_id)
        self._gate_loop = (
            GateLoop(
                services.pool,
                session_id,
                services.transcriber.name,
                services.gate,
                self._guard,
                self._stream,
            )
            if services.gate is not None
            else None
        )
        self._reconnect_max_s = reconnect_max_s
        self._queue: asyncio.Queue[AudioEvent | None] = asyncio.Queue()
        self._stopping = asyncio.Event()
        self._capture_handle: Capture | None = None
        self._capturing: asyncio.Task[None] | None = None
        self._transcribing: asyncio.Task[None] | None = None
        self._gating: asyncio.Task[None] | None = None
        # End of the last leg captured in this run. The database may not know it yet (its
        # spans can still be queued), so the next leg never starts before it.
        self._clock_ms = 0

    async def start(self) -> None:
        previous = await transcript.tail_text(self._pool, self.session_id, self._transcriber.name)
        self._transcribing = asyncio.create_task(self._transcribe(previous))
        self._capturing = asyncio.create_task(self._capture())
        if self._gate_loop is not None:
            self._gating = asyncio.create_task(self._gate_loop.run())

    def raise_if_failed(self) -> None:
        for task in (self._capturing, self._transcribing, self._gating):
            if (
                task is not None
                and task.done()
                and not task.cancelled()
                and (error := task.exception()) is not None
            ):
                raise error

    async def stop(
        self, *, session_ended: bool = False, drain_timeout_s: float = DRAIN_TIMEOUT_S
    ) -> None:
        """End the current leg cleanly (everything already received is archived and queued),
        let transcription drain the queue, then gate what it committed. With `session_ended`
        the trailing partial Window is gated too; on a shutdown a restart closes it at full
        length instead. Returns before a new run may start.

        A second cancellation of the caller (another SIGTERM) still propagates.
        """
        current = asyncio.current_task()
        cancels_before = current.cancelling() if current else 0
        self._stopping.set()
        if self._capture_handle is not None:
            await self._capture_handle.stop_input()
        if self._capturing is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._capturing), CAPTURE_STOP_TIMEOUT_S)
            except TimeoutError:
                log.warning("session %d: capture did not stop cleanly", self.session_id)
                self._capturing.cancel()
            except asyncio.CancelledError:
                if current is not None and current.cancelling() > cancels_before:
                    raise
            except Exception:
                log.exception("session %d: capture had failed", self.session_id)
        self._queue.put_nowait(None)
        log.info("session %d: capture stopped; draining transcription", self.session_id)
        if self._transcribing is not None:
            try:
                await asyncio.wait_for(self._transcribing, drain_timeout_s)
                log.info("session %d: drained", self.session_id)
            except TimeoutError:
                log.warning(
                    "session %d: drain timed out; queued audio stays untranscribed", self.session_id
                )
        if self._gate_loop is not None and self._gating is not None:
            self._gate_loop.finish(session_ended=session_ended)
            try:
                await asyncio.wait_for(self._gating, GATE_DRAIN_TIMEOUT_S)
            except TimeoutError:
                log.warning("session %d: Gate did not finish; restart resumes", self.session_id)
            except Exception:
                log.exception("session %d: Gate had failed", self.session_id)

    async def _capture(self) -> None:
        delay = 2.0
        while not self._stopping.is_set():
            try:
                captured = await self._capture_leg()
            except psycopg.OperationalError as e:
                log.warning("session %d: database error in capture (%s)", self.session_id, e)
                captured = False
            if self._stopping.is_set():
                return
            if captured:
                delay = 2.0
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)
            delay = min(delay * 2, self._reconnect_max_s)

    async def _capture_leg(self) -> bool:
        """Run one leg to its end. Returns whether it delivered any audio."""
        leg = await sessions.next_leg(self._pool, self._channel, self.session_id, self._clock_ms)
        path = self._data_dir / leg.archive_path
        samples = 0
        leftover = b""
        recorded = False
        try:
            async with self._source.capture(path) as capture:
                self._capture_handle = capture
                if self._stopping.is_set():
                    await capture.stop_input()
                async for data in capture.pcm:
                    if not recorded:
                        await sessions.record_leg(self._pool, leg, datetime.now(UTC))
                        recorded = True
                    data = leftover + data
                    whole = len(data) - len(data) % BYTES_PER_SAMPLE
                    leftover = data[whole:]
                    block = AudioBlock(leg.audio_start_ms * SAMPLES_PER_MS + samples, data[:whole])
                    samples += whole // BYTES_PER_SAMPLE
                    self._queue.put_nowait(block)
        finally:
            self._capture_handle = None
            if samples:
                end_ms = leg.audio_start_ms + samples // SAMPLES_PER_MS
                self._clock_ms = end_ms
                self._queue.put_nowait(LegEnded())
                log.info("session %d: leg %d ended at %d ms", self.session_id, leg.seq, end_ms)
                await sessions.end_leg(self._pool, leg, end_ms)
            else:
                path.unlink(missing_ok=True)
        return samples > 0

    async def _events(self) -> AsyncIterator[AudioEvent]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def _transcribe(self, previous_text: str) -> None:
        context = Context(self._vocabulary, self._guard, previous_text)
        async for result in self._transcriber.transcribe(self._events(), context):
            await self._commit(result)

    def _stream(self) -> dict[str, JSON]:
        """What the Gate is told about the stream: `context.stream`."""
        stream: dict[str, JSON] = {"title": self.meta.title}
        if self.meta.category:
            stream["category"] = self.meta.category
        return stream

    def _vocabulary(self) -> str:
        """Title-only until Packs supply real Vocabulary (#6)."""
        category = f"{self.meta.category} stream. " if self.meta.category else ""
        return f"{category}{self.meta.title}".strip()

    async def _commit(self, result: Result) -> None:
        """Retry the commit, never the paid call that produced the Result."""
        delay = 1.0
        while True:
            try:
                await transcript.commit(self._pool, self.session_id, self._transcriber.name, result)
                if self._gate_loop is not None:
                    self._gate_loop.wake()
                return
            except psycopg.OperationalError as e:
                log.warning("session %d: commit failed (%s); retrying", self.session_id, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
