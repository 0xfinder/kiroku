"""Source (D19): whether a channel is live, and its audio while it is.

A Source answers two questions: "is the channel live, and which broadcast?" (`probe`) and
"write this leg's archive and give me its PCM until the stream drops" (`capture`). It knows
nothing about sessions or clocks; reconnect policy lives in kiroku.watch.

Twitch is the only implementation (D21: streamlink metadata, no Twitch credentials). Tests
point StreamlinkSource at a fake streamlink executable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from kiroku.audio import FFMPEG_PCM, SAMPLE_RATE
from kiroku.errors import KirokuError

PROBE_TIMEOUT_S = 30


class ToolMissing(KirokuError):
    pass


READ_SIZE = 32_768
ARCHIVE_BITRATE = "32k"  # D17


@dataclass(frozen=True, slots=True)
class StreamMeta:
    """What the platform says about one broadcast. `stream_id` identifies the session: the
    same broadcast seen again after a blip or a restart resumes the same session."""

    stream_id: str
    title: str
    category: str | None


@dataclass(frozen=True, slots=True)
class Online:
    meta: StreamMeta


@dataclass(frozen=True, slots=True)
class NotLive:
    pass


@dataclass(frozen=True, slots=True)
class ProbeFailed:
    """The probe itself broke. It says nothing about whether the channel is live."""

    reason: str


type Probe = Online | NotLive | ProbeFailed


class Capture(Protocol):
    """One running leg."""

    @property
    def pcm(self) -> AsyncIterator[bytes]:
        """Raw PCM (kiroku.audio format) in arbitrary-sized pieces, until the leg ends."""
        ...

    async def stop_input(self) -> None:
        """End the leg cleanly: stop reading the stream, and let everything already
        received flow through `pcm` (and into the archive) before it ends."""
        ...


class Source(Protocol):
    @property
    def platform(self) -> str: ...

    @property
    def login(self) -> str: ...

    async def probe(self) -> Probe:
        """Never raises for platform trouble; that is ProbeFailed."""
        ...

    def capture(self, archive: Path) -> AbstractAsyncContextManager[Capture]:
        """One leg: write Opus to `archive` and stream its PCM until the stream drops or
        `stop_input()` is called. Exiting the context kills whatever is still running."""
        ...


class StreamlinkSource:
    """`streamlink --stdout twitch.tv/<login> audio_only | ffmpeg` -> Opus file + PCM.

    One ffmpeg writes both outputs, so the PCM byte count is exact for the archive too.
    Measured on this host: about 9% of one CPU and 65 MB RSS per channel (spike).
    """

    platform = "twitch"

    def __init__(
        self,
        login: str,
        *,
        streamlink: Sequence[str] = (sys.executable, "-m", "streamlink"),
        ffmpeg: str = "ffmpeg",
    ) -> None:
        self.login = login
        self._streamlink = tuple(streamlink)
        self._ffmpeg = ffmpeg

    async def probe(self) -> Probe:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._streamlink,
                "--json",
                f"twitch.tv/{self.login}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            return ProbeFailed(f"streamlink: {e!r}")
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
        except TimeoutError:
            return ProbeFailed(f"streamlink --json took over {PROBE_TIMEOUT_S} s")
        finally:
            # On timeout or cancellation, don't leave the process for the event loop's
            # garbage collector (that logs "Event loop is closed" at shutdown).
            await _stop(proc, grace_s=0)
        return parse_probe(stdout.decode(errors="replace"))

    def capture(self, archive: Path) -> AbstractAsyncContextManager[Capture]:
        return self._capture(archive)

    @asynccontextmanager
    async def _capture(self, archive: Path) -> AsyncGenerator[Capture]:
        archive.parent.mkdir(parents=True, exist_ok=True)
        read_fd, write_fd = os.pipe()
        try:
            streamlink = await asyncio.create_subprocess_exec(
                *self._streamlink,
                "--stdout",
                "--loglevel",
                "error",
                f"twitch.tv/{self.login}",
                "audio_only",
                stdout=write_fd,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                ffmpeg = await asyncio.create_subprocess_exec(
                    *ffmpeg_command(self._ffmpeg, archive),
                    stdin=read_fd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except BaseException:
                await _stop(streamlink, grace_s=0)
                raise
        except FileNotFoundError as e:
            raise ToolMissing(f"{e.filename} not found; install it or fix PATH") from e
        finally:
            # Our copies must close, so ffmpeg sees EOF when streamlink exits.
            os.close(read_fd)
            os.close(write_fd)
        try:
            yield _StreamlinkCapture(streamlink, ffmpeg)
        finally:
            # Stop the input first: ffmpeg then reaches EOF and finishes the Ogg file.
            await _stop(streamlink, grace_s=0)
            await _stop(ffmpeg, grace_s=5)


class _StreamlinkCapture:
    def __init__(
        self, streamlink: asyncio.subprocess.Process, ffmpeg: asyncio.subprocess.Process
    ) -> None:
        assert ffmpeg.stdout is not None
        self._streamlink = streamlink
        self._stdout = ffmpeg.stdout

    @property
    def pcm(self) -> AsyncIterator[bytes]:
        return self._read()

    async def _read(self) -> AsyncIterator[bytes]:
        while data := await self._stdout.read(READ_SIZE):
            yield data

    async def stop_input(self) -> None:
        await _stop(self._streamlink, grace_s=0)


def ffmpeg_command(ffmpeg: str, archive: Path) -> list[str]:
    """One input, two outputs: the Opus archive (D17) and PCM on stdout."""
    return [
        ffmpeg,
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "libopus",
        "-b:a",
        ARCHIVE_BITRATE,
        "-f",
        "ogg",
        # Write each packet as it's encoded. ffmpeg otherwise buffers 256 KB (about a
        # minute of 32 kbps Opus), which a crash would lose from the archive.
        "-flush_packets",
        "1",
        "-y",
        str(archive),
        "-vn",
        *FFMPEG_PCM,
        "pipe:1",
    ]


async def _stop(proc: asyncio.subprocess.Process, *, grace_s: float) -> None:
    """Wait briefly for a clean exit, then SIGTERM, then SIGKILL the process group.

    A child with a PIPE stdout is reaped with communicate(), never wait(): the PCM
    reader may have stopped, and on Python 3.12 wait() also waits for the stdout
    transport to close, which that unread pipe never does. communicate() drains it to
    EOF, and nobody needs the drained bytes after teardown. A child whose stdout is
    redirected to a file descriptor has no transport to stall, so it keeps wait()'s
    kill ordering untouched.
    """
    if proc.returncode is not None and proc.stdout is not None:
        await proc.communicate()
        return
    reap = proc.communicate if proc.stdout is not None else proc.wait
    for sig, wait_s in ((None, grace_s), (signal.SIGTERM, grace_s), (signal.SIGKILL, None)):
        if sig is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(reap(), wait_s)
            return
        except TimeoutError:
            continue


def parse_probe(stdout: str) -> Probe:
    """`streamlink --json` output -> Probe.

    Live: {"metadata": {"id", "author", "category", "title"}, "streams": {...}}.
    Offline: {"error": "No playable streams found on this URL: ..."}.
    Anything else is ProbeFailed: an unknown answer never counts as offline.
    """
    try:
        parsed: object = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return ProbeFailed(f"not JSON: {stdout[:200]!r}")
    if not isinstance(parsed, dict):
        return ProbeFailed(f"unexpected JSON: {stdout[:200]!r}")
    body = cast(dict[str, Any], parsed)
    error = body.get("error")
    if isinstance(error, str):
        if "No playable streams" in error:
            return NotLive()
        return ProbeFailed(error)
    meta = body.get("metadata")
    if not isinstance(meta, dict) or not cast(dict[str, Any], meta).get("id"):
        return ProbeFailed(f"no stream id in: {stdout[:200]!r}")
    meta = cast(dict[str, Any], meta)
    category = meta.get("category")
    return Online(
        StreamMeta(
            stream_id=str(meta["id"]),
            title=str(meta.get("title") or ""),
            category=str(category) if category else None,
        )
    )
