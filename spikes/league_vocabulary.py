"""Compare title-only and League Vocabulary using production Groq and its Cost ledger.

uv run python spikes/league_vocabulary.py /tmp/caedrel.pcm <outdir>
Use KIROKU_DATABASE_URL for a migrated spike database. Audio is mono 16 kHz s16le.
The completed match fixture is only used to seed the recorded clip's Vocabulary.
"""

import asyncio
import json
import sys
import os
from pathlib import Path

import httpx

from kiroku import ledger
from kiroku.audio import AudioBlock, LegEnded
from kiroku.budget import Budget
from kiroku.config import SecretName, Settings
from kiroku.db import open_pool
from kiroku.pack import LiveContext
from kiroku.packs.league import vocabulary
from kiroku.packs.league.data import match_context, names, roster_terms
from kiroku.transcriber import Context
from kiroku.transcriber.groq import GroqTranscriber, build_prompt

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/league"
TITLE = "DEMACIA CUP FLY VS RED - LAST SPOTS IN THE TOP 8"


def read(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


async def run(audio: Path, out: Path):
    settings = Settings.load()
    event = read("getEventDetails")["data"]["event"]
    event["match"]["games"][0]["state"] = "inProgress"
    champions, items = names(read("champion")), names(read("item"))
    summary, key, terms = match_context(event, read("window"), champions)
    live = LiveContext(summary, key, terms)
    pack = vocabulary(TITLE, champions, items, roster_terms(read("getTeams"), TITLE), live)
    pcm = audio.read_bytes()
    out.mkdir(parents=True, exist_ok=True)
    async with open_pool(settings.database_url) as pool, httpx.AsyncClient(timeout=60) as http:
        groq = GroqTranscriber(http, settings.secrets.require(SecretName.GROQ))
        for name, words in [("title", f"League of Legends stream. {TITLE}"), ("pack", pack)]:
            if os.environ.get("KIROKU_SPIKE_PACK_ONLY") and name == "title":
                continue
            async def events():
                for i in range(0, len(pcm), 32000):
                    yield AudioBlock(i // 2, pcm[i:i + 32000])
                yield LegEnded()

            tail = ""
            with (out / f"{name}.jsonl").open("w") as f:
                async for result in groq.transcribe(events(), Context(lambda: words, Budget(pool, settings.caps).guard(None))):
                    text = " ".join(u.text for u in result.utterances)
                    if result.cost:
                        async with pool.connection() as conn:
                            await ledger.record(conn, result.cost)
                    row = {"start_ms": result.start_ms, "end_ms": result.end_ms,
                           "prompt": build_prompt(words, tail), "text": text,
                           "usd": str(result.cost.usd) if result.cost else "0"}
                    f.write(json.dumps(row) + "\n")
                    f.flush()
                    tail = (tail + " " + text).strip() if text else tail
                    print(name, result.start_ms, text[:100], flush=True)


asyncio.run(run(Path(sys.argv[1]), Path(sys.argv[2])))
