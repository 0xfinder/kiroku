import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

import pytest

from kiroku import archive
from kiroku.source import (
    NotLive,
    Online,
    ProbeFailed,
    StreamlinkSource,
    StreamMeta,
    ffmpeg_command,
    parse_probe,
)

FAKE_STREAMLINK = Path(__file__).parent / "fakes" / "streamlink.py"


def test_live_probe_carries_the_broadcast_id() -> None:
    # Shape recorded from `streamlink --json twitch.tv/gorgc` on 2026-10-11.
    out = json.dumps(
        {
            "metadata": {
                "id": "318767459047",
                "author": "Gorgc",
                "category": "Dota 2",
                "title": "Spirit vs Yandex",
            },
            "streams": {"audio_only": {}},
        }
    )
    assert parse_probe(out) == Online(StreamMeta("318767459047", "Spirit vs Yandex", "Dota 2"))


def test_offline_probe() -> None:
    out = json.dumps({"error": "No playable streams found on this URL: twitch.tv/caedrel"})
    assert parse_probe(out) == NotLive()


def test_unknown_answers_never_count_as_offline() -> None:
    assert isinstance(parse_probe("Traceback (most recent call last):"), ProbeFailed)
    assert isinstance(parse_probe(json.dumps({"error": "Unable to open URL"})), ProbeFailed)
    assert isinstance(parse_probe(json.dumps({"metadata": {}})), ProbeFailed)
    assert isinstance(parse_probe("[]"), ProbeFailed)


def test_ffmpeg_writes_the_archive_and_pcm_from_one_input() -> None:
    cmd = ffmpeg_command("ffmpeg", Path("/data/audio/gorgc/1/001.opus"))
    assert cmd.count("-i") == 1
    archive_at = cmd.index("/data/audio/gorgc/1/001.opus")
    assert cmd[archive_at - 3 : archive_at] == ["-flush_packets", "1", "-y"]
    assert cmd[-1] == "pipe:1"


async def test_missing_ffmpeg_is_a_named_error_and_leaves_no_streamlink(tmp_path: Path) -> None:
    import sys

    import pytest

    from kiroku.source import StreamlinkSource, ToolMissing

    fake = Path(__file__).parent / "fakes" / "streamlink.py"
    source = StreamlinkSource(
        "x", streamlink=(sys.executable, str(fake)), ffmpeg="/nonexistent/ffmpeg"
    )
    with pytest.raises(ToolMissing, match="/nonexistent/ffmpeg"):
        async with source.capture(tmp_path / "a.opus"):
            pass


# ---- teardown (real Source context): a PCM reader that stops early -------------------------


def _fake_source(tmp_path: Path, state: str, monkeypatch: pytest.MonkeyPatch) -> StreamlinkSource:
    """One fake streamlink with its own leg counter, streaming one 12-second leg."""
    state_dir = tmp_path / state
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("FAKE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("FAKE_LEGS", "12")
    return StreamlinkSource("fake", streamlink=(sys.executable, str(FAKE_STREAMLINK)))


async def test_teardown_drains_a_paused_pcm_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader stops mid-stream and ffmpeg keeps producing into an unread pipe.
    Teardown must still finish within its deadline: reap, don't stall (P2 defect)."""
    source = _fake_source(tmp_path, "paused", monkeypatch)
    cm = source.capture(tmp_path / "paused" / "001.opus")
    taken = b""
    async with asyncio.timeout(30):
        capture = await cm.__aenter__()
        stream = cast(AsyncGenerator[bytes, None], capture.pcm)
        async for block in stream:
            taken += block
            if len(taken) >= 64_000:
                break
        await stream.aclose()  # the reader stops here; audio is still in flight
        await cm.__aexit__(None, None, None)
    paused_ms = await archive.decoded_ms(tmp_path / "paused" / "001.opus")
    assert taken and paused_ms is not None and 0 < paused_ms < 12_000

    # Control: a full read of the same leg; every byte the paused reader took equals
    # the same prefix of the complete stream, so the drain stole nothing delivered.
    source = _fake_source(tmp_path, "full", monkeypatch)
    async with asyncio.timeout(30):
        async with source.capture(tmp_path / "full" / "001.opus") as capture:
            blocks = [block async for block in capture.pcm]
    whole = b"".join(blocks)
    assert await archive.decoded_ms(tmp_path / "full" / "001.opus") == 12_000
    assert whole[: len(taken)] == taken
