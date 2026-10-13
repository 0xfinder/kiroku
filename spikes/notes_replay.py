"""Issue #7: real Groq Notes over both stored fixtures, three repeats."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from alerts_common import Recorder, scratch

from kiroku import notes
from kiroku.llm.groq import GroqLLM
from kiroku.budget import Budget, Caps
from kiroku.config import SecretName, Settings
from kiroku.db import open_pool
from kiroku.packs.league import LeaguePack
from kiroku.packs.league.data import PUBLIC_WEBSITE_KEY
from tests.fakes.notes import TRANSCRIBER, seed


async def main(out, repeats, database):
    settings = Settings.load()
    url = await scratch(database)
    recorder = Recorder(pace_s=16)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"header": {"model": "openai/gpt-oss-120b",
                                        "database": database, "repeats": repeats,
                                        "pace_s": 16}}) + "\n")
    async with open_pool(url) as pool, httpx.AsyncClient(transport=recorder) as http:
        llm = GroqLLM(http, settings.secrets.require(SecretName.GROQ))
        pack = LeaguePack(http, settings.data_dir, PUBLIC_WEBSITE_KEY)
        total = 0
        for repeat in range(repeats):
            for fixture in ("league", "general"):
                channel, session, now = await seed(pool, url, fixture)
                worker = notes.NotesWorker(pool, [channel], TRANSCRIBER, llm,
                                           Budget(pool, Caps()), [pack], now=lambda: now)
                for pass_number in range(20):
                    await worker.drain()
                    if not await notes.pending(pool, [channel], TRANSCRIBER, notes.NotesPolicy(), now, gated=True):
                        break
                    print(fixture, repeat, "pending; retrying", flush=True)
                    await asyncio.sleep(16)
                else:
                    raise RuntimeError("Notes did not complete after 20 passes")
                rows = await notes.rows(pool, session_id=session)
                async with pool.connection() as conn:
                    cost = await (await conn.execute(
                        "SELECT count(*), coalesce(sum(usd),0) FROM cost_ledger "
                        "WHERE session_id=%s AND purpose='notes'", [session]
                    )).fetchone()
                with out.open("a") as f:
                    for call in recorder.rows:
                        f.write(json.dumps({"fixture": fixture, "repeat": repeat, **call}) + "\n")
                    f.write(json.dumps({"result": {"fixture": fixture, "repeat": repeat,
                                                  "notes": rows, "ledger": cost}}, default=str) + "\n")
                recorder.rows.clear()
                total += cost[1]
                print(fixture, repeat, [r["status"] for r in rows], cost, flush=True)
        print("total Notes USD:", total, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--database", default="kiroku_spike_7_notes")
    a = p.parse_args()
    asyncio.run(main(a.out, a.repeats, a.database))
