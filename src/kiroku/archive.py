"""Reading the audio archive back: a leg file's length, and the PCM of a range of it.

Archive files are the Ogg Opus that capture writes (kiroku.source.ffmpeg_command), one per
Capture leg. Offsets here are relative to the file's own start, not the session clock.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from kiroku.audio import BYTES_PER_SAMPLE, FFMPEG_PCM, SAMPLES_PER_MS
from kiroku.errors import KirokuError
from kiroku.source import READ_SIZE, ToolMissing

PREROLL_MS = 500


PROBE_TIMEOUT_S = 10.0  # ffprobe reads one page at the end of a local file: about 80 ms


class ArchiveUnreadable(KirokuError):
    pass


class LengthUnknown(KirokuError):
    """ffprobe didn't finish, so nothing is known about the file. Try again later."""


async def decoded_ms(path: Path, *, ffprobe: str = "ffprobe") -> int | None:
    """How many ms of audio a full decode of `path` returns. None when it holds nothing
    decodable: the file is missing, or ffprobe rejects it (then ffmpeg can't decode it
    either). Raises LengthUnknown when ffprobe couldn't finish (timed out, killed, couldn't
    start): the file may be fine, so ask again later rather than conclude anything.

    Read from the last Ogg page's granule position, so it costs one seek, not a decode (a
    full decode runs at about 130x real time here: minutes for a 6-hour leg). ffprobe's
    `duration` would overstate it by Opus's pre-skip (6.5 ms); `duration_ts` minus
    `initial_padding` matches a full decode exactly, also for a file a crash cut mid-page.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration_ts,initial_padding,time_base",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise ToolMissing(f"{e.filename} not found; install it or fix PATH") from e
    except OSError as e:
        raise LengthUnknown(f"ffprobe could not start: {e}") from e
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
    except TimeoutError:
        raise LengthUnknown(f"ffprobe took over {PROBE_TIMEOUT_S} s on {path}") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
    if proc.returncode is not None and proc.returncode < 0:
        raise LengthUnknown(f"ffprobe was killed by signal {-proc.returncode}")
    if proc.returncode != 0:
        return None
    try:
        streams = cast(list[dict[str, Any]], json.loads(stdout)["streams"])
        stream = streams[0]
        ticks = int(stream["duration_ts"]) - int(stream.get("initial_padding", 0))
        tick = Fraction(str(stream["time_base"]))
    except (KeyError, IndexError, ValueError, ZeroDivisionError):
        return None
    return max(int(ticks * tick * 1000), 0)


async def decode(
    path: Path, start_ms: int, end_ms: int, *, ffmpeg: str = "ffmpeg"
) -> AsyncGenerator[bytes]:
    """PCM (kiroku.audio format) of [start_ms, end_ms) of `path`, in whole samples.

    Yields at most end_ms - start_ms of audio; less if the file ends sooner. Raises
    ArchiveUnreadable if ffmpeg fails.

    Seeking with `-ss` before `-i` skips decoding the file up to the seek point, and lands
    on the right sample, but the decoder needs about 200 ms after a seek to match a
    continuous decode. So it seeks PREROLL_MS early and drops that much exactly (`-ss` after
    `-i` decodes and discards). The result matches a full decode of the file.
    """
    want = (end_ms - start_ms) * SAMPLES_PER_MS * BYTES_PER_SAMPLE
    preroll_ms = min(PREROLL_MS, start_ms)
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{(start_ms - preroll_ms) / 1000:.3f}",
            "-i",
            str(path),
            "-ss",
            f"{preroll_ms / 1000:.3f}",
            "-t",
            f"{(end_ms - start_ms) / 1000:.3f}",
            "-vn",
            *FFMPEG_PCM,
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise ToolMissing(f"{e.filename} not found; install it or fix PATH") from e
    assert proc.stdout is not None
    sent = 0
    leftover = b""
    try:
        while sent < want and (data := await proc.stdout.read(READ_SIZE)):
            data = leftover + data
            whole = min(len(data) - len(data) % BYTES_PER_SAMPLE, want - sent)
            leftover = data[whole:]
            sent += whole
            yield data[:whole]
        if sent < want and await proc.wait() != 0:
            raise ArchiveUnreadable(f"ffmpeg could not decode {path} from {start_ms} ms")
    finally:
        if proc.returncode is None:
            proc.kill()
        # Not wait(): it also waits for stdout to close, which never happens while unread
        # output has paused the pipe. communicate() reads it to EOF.
        await proc.communicate()
