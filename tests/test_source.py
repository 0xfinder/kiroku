import json
from pathlib import Path

from kiroku.source import NotLive, Online, ProbeFailed, StreamMeta, ffmpeg_command, parse_probe


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
