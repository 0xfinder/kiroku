"""`kiroku watch fake` in a process of its own, so a test can SIGKILL it mid-session.

Usage: watch_proc.py DATABASE_URL DATA_DIR. The fake streamlink is driven by the FAKE_*
environment (tests/fakes/streamlink.py). Groq is a stub that takes FAKE_GROQ_DELAY seconds
per call, so audio queues up behind it and a kill loses transcription, not archive.
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx

from kiroku.budget import Budget, Caps
from kiroku.config import Secret
from kiroku.db import open_pool
from kiroku.sessions import claim_channels
from kiroku.source import StreamlinkSource
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import watch

FAKE_STREAMLINK = Path(__file__).parent / "streamlink.py"
calls = 0


async def groq(request: httpx.Request) -> httpx.Response:
    global calls
    calls += 1
    n = calls
    await asyncio.sleep(float(os.environ.get("FAKE_GROQ_DELAY", "0")))
    segment = {"no_speech_prob": 0.0, "avg_logprob": -0.1}
    return httpx.Response(
        200,
        json={
            "segments": [
                {**segment, "start": 0.2, "end": 1.0, "text": f"killed run {n} first"},
                {**segment, "start": 1.5, "end": 3.0, "text": f"killed run {n} second"},
            ]
        },
    )


async def main(database_url: str, data_dir: Path) -> None:
    async with (
        open_pool(database_url) as pool,
        claim_channels(database_url, "twitch", ["fake"]) as channels,
        httpx.AsyncClient(transport=httpx.MockTransport(groq)) as http,
    ):
        source = StreamlinkSource("fake", streamlink=(sys.executable, str(FAKE_STREAMLINK)))
        await watch(
            pool,
            [(channels[0], source)],
            GroqTranscriber(http, Secret("k")),
            data_dir,
            budget=Budget(pool, Caps()),
            gate=None,
            poll_s=0.5,
        )


asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
