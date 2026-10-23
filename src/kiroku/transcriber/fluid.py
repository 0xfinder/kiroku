"""Local Fluid Speech Mini behind the Transcriber interface; one worker per watcher."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kiroku.audio import ms
from kiroku.errors import KirokuError
from kiroku.transcriber import Context, Result, Utterance
from kiroku.transcriber.chunker import Chunk, ChunkPolicy
from kiroku.transcriber.fluid_assets import REVISION
from kiroku.transcriber.queued import ChunkedTranscriber


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_ms: int
    end_ms: int
    text: str


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ready: bool = False
    error: str | None = None
    segments: list[Segment] | None = None


class FluidWorker:
    """A serial JSON-lines subprocess, killable even when native inference hangs."""

    def __init__(self, command: Sequence[str], *, timeout_s: float = 60) -> None:
        self._command = tuple(command)
        self._timeout_s = timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._closing: asyncio.Task[None] | None = None

    async def _reply(self) -> Reply:
        assert self._process is not None and self._process.stdout is not None
        line = await self._process.stdout.readline()
        if not line:
            raise KirokuError("Fluid worker exited without a response")
        reply = Reply.model_validate_json(line)
        if reply.error:
            raise KirokuError(f"Fluid worker: {reply.error}")
        return reply

    async def start(self) -> None:
        if self._process is not None:
            return
        try:
            async with asyncio.timeout(self._timeout_s):
                self._process = await asyncio.create_subprocess_exec(
                    *self._command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                reply = await self._reply()
                if not reply.ready or reply.segments is not None:
                    raise KirokuError("Fluid worker did not become ready")
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            await self.close()
            raise KirokuError(f"Fluid startup failed: {error}") from error

    async def infer(self, pcm: bytes) -> list[Segment]:
        try:
            await self.start()
            assert self._process is not None and self._process.stdin is not None
            async with asyncio.timeout(self._timeout_s):
                payload = json.dumps({"pcm": base64.b64encode(pcm).decode("ascii")}) + "\n"
                self._process.stdin.write(payload.encode())
                await self._process.stdin.drain()
                reply = await self._reply()
                if reply.ready or reply.segments is None:
                    raise KirokuError("Fluid worker returned no transcript")
                return reply.segments
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            await self.close()
            raise KirokuError(f"Fluid inference failed: {error}") from error

    async def close(self) -> None:
        process = self._process
        if process is None:
            return
        if self._closing is None:
            self._closing = asyncio.create_task(self._reap(process))
        closing = self._closing
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(closing)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    # A second shutdown signal skips the grace period but still reaps.
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
        finally:
            if closing.done():
                self._process = None
                self._closing = None
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), 2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


class FluidTranscriber(ChunkedTranscriber):
    def __init__(
        self,
        worker: FluidWorker,
        *,
        policy: ChunkPolicy | None = None,
        max_backlog_ms: int = 30 * 60_000,
    ) -> None:
        super().__init__(policy, max_backlog_ms)
        self._worker = worker
        self._inference = asyncio.Lock()

    @property
    def name(self) -> str:
        return f"fluid:speech-mini:{REVISION[:12]}:sentences-v1"

    @classmethod
    def local(cls, directory: Path, threads: int) -> FluidTranscriber:
        return cls(
            FluidWorker(
                [
                    sys.executable,
                    "-m",
                    "kiroku.transcriber.fluid_worker",
                    str(directory),
                    str(threads),
                ]
            )
        )

    async def start(self) -> None:
        await self._worker.start()

    async def close(self) -> None:
        await self._worker.close()

    async def _transcribe_chunk(self, chunk: Chunk, context: Context, tail: str) -> Result:
        while True:
            if context.background:
                await self._live_idle.wait()
            await self._inference.acquire()
            if not context.background or self._live_idle.is_set():
                break
            self._inference.release()
        try:
            segments = await self._worker.infer(chunk.pcm)
        finally:
            self._inference.release()
        start, end = ms(chunk.start_sample), ms(chunk.end_sample)
        out: list[Utterance] = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            if not 0 <= segment.start_ms <= segment.end_ms <= end - start:
                raise KirokuError("Fluid returned timestamps outside its source chunk")
            s = min(start + segment.start_ms, end - 1)
            e = min(max(start + segment.end_ms, s + 1), end)
            if out and s <= out[-1].start_ms:
                previous = out.pop()
                out.append(
                    Utterance(
                        previous.start_ms, max(previous.end_ms, e), previous.text + " " + text
                    )
                )
            else:
                out.append(Utterance(s, e, text))
        return Result(start, end, tuple(out), None)
