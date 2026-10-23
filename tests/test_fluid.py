# pyright: reportPrivateUsage=false
import asyncio
import hashlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from kiroku.audio import AudioBlock, AudioEvent
from kiroku.budget import Unlimited
from kiroku.cli import _open_transcriber, app
from kiroku.config import ConfigError, Settings, TranscriptionProvider
from kiroku.errors import KirokuError
from kiroku.transcriber import Context
from kiroku.transcriber import fluid_assets as assets
from kiroku.transcriber.fluid import FluidTranscriber, FluidWorker
from tests.fakes.speech import speechlike


def worker(body: str, *, timeout: float = 2) -> FluidWorker:
    return FluidWorker([sys.executable, "-u", "-c", body], timeout_s=timeout)


def responding(segments: list[dict[str, object]]) -> FluidWorker:
    return worker(
        'import sys,json\nprint(json.dumps({"ready":True}),flush=True)\n'
        f"for line in sys.stdin: print({json.dumps({'segments': segments})!r},flush=True)\n"
    )


async def events(pcm: bytes, start: int = 0) -> AsyncIterator[AudioEvent]:
    yield AudioBlock(start, pcm)


async def test_fluid_maps_sentences_to_session_clock_without_prompt_or_cost() -> None:
    provider = FluidTranscriber(
        responding(
            [
                {"start_ms": 80, "end_ms": 160, "text": " Xayah. "},
                {"start_ms": 80, "end_ms": 240, "text": "Blink dagger."},
                {"start_ms": 400, "end_ms": 500, "text": "Roshan!"},
            ]
        )
    )
    pcm, _ = speechlike(2, seed=1)

    def vocabulary() -> str:
        raise AssertionError("Fluid cannot accept a prompt")

    try:
        results = [
            r
            async for r in provider.transcribe(
                events(pcm, 160_000), Context(vocabulary, Unlimited(), previous_text="ignored")
            )
        ]
    finally:
        await provider.close()
    assert len(results) == 1
    result = results[0]
    assert (result.start_ms, result.end_ms, result.cost) == (10_000, 12_000, None)
    assert [(u.start_ms, u.end_ms, u.text) for u in result.utterances] == [
        (10_080, 10_240, "Xayah. Blink dagger."),
        (10_400, 10_500, "Roshan!"),
    ]


async def test_silence_never_starts_a_worker() -> None:
    provider = FluidTranscriber(worker('raise RuntimeError("must not run")'))
    results = [
        r
        async for r in provider.transcribe(events(bytes(64_000)), Context(lambda: "", Unlimited()))
    ]
    assert [(r.start_ms, r.end_ms, r.utterances, r.cost) for r in results] == [(0, 2000, (), None)]


@pytest.mark.parametrize(
    "reply", ["not json", "{}", '{"error":"decoder failed"}', '{"segments":[{"text":1}]}']
)
async def test_worker_rejects_bad_responses_and_reaps_process(reply: str) -> None:
    instance = worker(
        "import sys\nprint('{\"ready\":true}',flush=True)\nsys.stdin.readline()\n"
        + f"print({reply!r},flush=True)"
    )
    await instance.start()
    process = instance._process
    assert process is not None
    with pytest.raises(KirokuError):
        await instance.infer(bytes(320))
    assert process.returncode is not None
    await instance.close()


@pytest.mark.parametrize("cancel", [True, False])
async def test_hung_native_inference_is_killed_and_reaped(cancel: bool) -> None:
    instance = worker(
        "import sys,time\nprint('{\"ready\":true}',flush=True)\n"
        "sys.stdin.readline()\ntime.sleep(60)",
        timeout=0.3,
    )
    await instance.start()
    process = instance._process
    assert process is not None
    task = asyncio.create_task(instance.infer(bytes(320)))
    if cancel:
        await asyncio.sleep(0.02)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else KirokuError):
        await asyncio.wait_for(task, 3)
    assert process.returncode is not None


async def test_startup_failure_is_actionable() -> None:
    instance = worker('print(\'{"error":"Install with uv sync --extra fluid"}\',flush=True)')
    with pytest.raises(KirokuError, match="uv sync --extra fluid"):
        await instance.start()


async def test_out_of_chunk_timestamps_fail_instead_of_corrupting_transcript() -> None:
    provider = FluidTranscriber(responding([{"start_ms": 0, "end_ms": 5000, "text": "bad"}]))
    pcm, _ = speechlike(2, seed=2)
    try:
        with pytest.raises(KirokuError, match="outside"):
            _ = [
                r async for r in provider.transcribe(events(pcm), Context(lambda: "", Unlimited()))
            ]
    finally:
        await provider.close()


def test_install_is_verified_atomic_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = b"weights"
    monkeypatch.setattr(assets, "FILES", {"encoder.onnx": hashlib.sha256(good).hexdigest()})
    destination = tmp_path / "encoder.onnx"
    destination.write_bytes(b"old invalid weights")
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"bad"))
        ) as http,
        pytest.raises(KirokuError, match="checksum"),
    ):
        assets.install(http, tmp_path)
    assert destination.read_bytes() == b"old invalid weights"
    assert list(tmp_path.iterdir()) == [destination]
    calls: list[httpx.Request] = []

    def download(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=good)

    with httpx.Client(transport=httpx.MockTransport(download)) as http:
        assets.install(http, tmp_path)
        assets.install(http, tmp_path)
    assert len(calls) == 1 and assets.REVISION in str(calls[0].url)
    assets.verify(tmp_path)
    destination.write_bytes(b"changed")
    with pytest.raises(KirokuError, match="missing or changed"):
        assets.verify(tmp_path)


async def test_fluid_factory_needs_no_api_keys_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = FluidTranscriber(responding([]))

    def verify(directory: Path) -> None:
        pass

    def local(directory: Path, threads: int) -> FluidTranscriber:
        return instance

    monkeypatch.setattr(assets, "verify", verify)
    monkeypatch.setattr(FluidTranscriber, "local", local)
    settings = Settings.load({"KIROKU_SECRETS_FILE": "/absent", "KIROKU_DATA_DIR": str(tmp_path)})
    async with httpx.AsyncClient() as http:
        async with _open_transcriber(settings, TranscriptionProvider.FLUID, http) as opened:
            assert opened is instance
            process = instance._worker._process
            assert process is not None and process.returncode is None
        assert process.returncode is not None
        with pytest.raises(ConfigError, match="GROQ_API_KEY"):
            async with _open_transcriber(settings, TranscriptionProvider.GROQ, http):
                pass


def test_provider_settings_and_cli_options() -> None:
    assert (
        Settings.load({"KIROKU_SECRETS_FILE": "/absent"}).transcriber == TranscriptionProvider.GROQ
    )
    settings = Settings.load(
        {
            "KIROKU_SECRETS_FILE": "/absent",
            "KIROKU_TRANSCRIBER": "fluid",
            "KIROKU_FLUID_THREADS": "1",
        }
    )
    assert settings.transcriber == TranscriptionProvider.FLUID and settings.fluid_threads == 1
    for env in (
        {"KIROKU_TRANSCRIBER": "unknown"},
        {"KIROKU_FLUID_THREADS": "0"},
        {"KIROKU_FLUID_THREADS": "x"},
    ):
        with pytest.raises(ConfigError):
            Settings.load({"KIROKU_SECRETS_FILE": "/absent"} | env)
    runner = CliRunner()
    for command in ("watch", "backfill"):
        result = runner.invoke(app, [command, "--help"], color=False)
        assert result.exit_code == 0
        assert "transcriber" in result.output


async def test_two_live_channels_share_worker_and_backfill_yields() -> None:
    instance = worker(
        "import sys,json,time\nprint('{\"ready\":true}',flush=True)\n"
        "for i,line in enumerate(sys.stdin,1):\n"
        " time.sleep(0.02)\n"
        ' print(json.dumps({"segments":[{"start_ms":0,"end_ms":80,"text":str(i)}]}),flush=True)'
    )
    from kiroku.transcriber.chunker import ChunkPolicy

    provider = FluidTranscriber(
        instance, policy=ChunkPolicy(min_ms=1000, max_ms=1000, forced_window_ms=100)
    )
    pcm, _ = speechlike(3, seed=5)

    async def collect(start: int, background: bool = False) -> list[str]:
        return [
            u.text
            async for r in provider.transcribe(
                events(pcm, start), Context(lambda: "", Unlimited(), background=background)
            )
            for u in r.utterances
        ]

    try:
        await provider.start()
        first = asyncio.create_task(collect(0))
        second = asyncio.create_task(collect(160_000))
        await asyncio.sleep(0)
        old = asyncio.create_task(collect(320_000, True))
        a, b, background = await asyncio.wait_for(asyncio.gather(first, second, old), 5)
    finally:
        await provider.close()
    assert a and b and background
    assert sorted(map(int, a + b)) == list(range(1, len(a + b) + 1))
    assert list(map(int, background)) == list(range(len(a + b) + 1, len(a + b + background) + 1))


async def test_fluid_archive_backfill_commits_once_without_paid_cost(
    database_url: str, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from kiroku import backfill, sessions, transcript
    from kiroku.db import open_pool
    from kiroku.source import StreamMeta
    from tests.fakes.speech import wav

    provider = FluidTranscriber(responding([{"start_ms": 80, "end_ms": 160, "text": "Roshan."}]))
    try:
        async with (
            open_pool(database_url) as pool,
            sessions.claim_channels(database_url, "twitch", ["gorgc"]) as channels,
        ):
            channel = channels[0]
            session = await sessions.open_session(
                pool, channel, StreamMeta("fluid-test", "Dota", "Dota 2"), datetime.now(UTC)
            )
            leg = await sessions.next_leg(pool, channel, session)
            await sessions.record_leg(pool, leg, datetime.now(UTC))
            pcm, _ = speechlike(2, seed=7)
            path = tmp_path / leg.archive_path
            path.parent.mkdir(parents=True)
            encode = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-c:a",
                "libopus",
                str(path),
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, errors = await encode.communicate(wav(pcm))
            assert encode.returncode == 0, errors
            await sessions.end_leg(pool, leg, 2000)
            for _ in range(2):
                assert await backfill.run(
                    pool, session, provider, tmp_path, lambda: "", Unlimited()
                )
            assert await backfill.gaps(pool, session, provider.name) == []
            assert await transcript.spans(pool, session, provider.name) == [(0, 2000)]
            async with pool.connection() as conn:
                assert await (
                    await conn.execute("SELECT text,transcriber FROM utterance")
                ).fetchall() == [("Roshan.", provider.name)]
                assert await (
                    await conn.execute("SELECT count(*) FROM cost_ledger")
                ).fetchone() == (0,)
    finally:
        await provider.close()


async def test_cancellation_during_close_kills_stubborn_worker_before_reraising() -> None:
    instance = worker(
        "import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "print('{\"ready\":true}',flush=True)\ntime.sleep(60)"
    )
    await instance.start()
    process = instance._process
    assert process is not None
    closing = asyncio.create_task(instance.close())
    await asyncio.sleep(0.02)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 3)
    assert process.returncode is not None
    await instance.close()


async def test_worker_survives_broadcast_stop_signals_until_supervisor_closes(
    tmp_path: Path,
) -> None:
    import signal

    entered, release = tmp_path / "entered", tmp_path / "release"
    instance = worker(
        "import sys,types,time\nfrom pathlib import Path\n"
        "runtime=types.ModuleType('kiroku.transcriber._fluid_onnx')\n"
        "class Model:\n"
        " def __init__(self,*args,**kwargs): pass\n"
        " def transcribe(self,pcm):\n"
        f"  Path({str(entered)!r}).touch()\n"
        f"  while not Path({str(release)!r}).exists(): time.sleep(0.005)\n"
        "  return []\n"
        "runtime.Model=Model\n"
        "sys.modules[runtime.__name__]=runtime\n"
        "sys.argv=['worker','unused','2']\n"
        "from kiroku.transcriber.fluid_worker import main\nmain()"
    )
    await instance.start()
    process = instance._process
    assert process is not None
    inference = asyncio.create_task(instance.infer(bytes(320)))
    try:
        async with asyncio.timeout(2):
            while not entered.exists():  # noqa: ASYNC110 - barrier shared with a subprocess
                await asyncio.sleep(0.005)
        process.send_signal(signal.SIGINT)
        process.send_signal(signal.SIGTERM)
        release.touch()
        assert await inference == []
    finally:
        inference.cancel()
        await asyncio.gather(inference, return_exceptions=True)
        await instance.close()
    assert process.returncode == 0
