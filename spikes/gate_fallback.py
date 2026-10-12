"""Issue #5: three production LLMGate repeats against Jev's eight split Windows."""

import argparse
import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
from alerts_common import Recorder, scratch

from kiroku import ledger
from kiroku.budget import Budget, Caps
from kiroku.config import SecretName, Settings
from kiroku.db import open_pool
from kiroku.gate import GateRequest, RuleQuestion
from kiroku.gate.llm import LLMGate
from kiroku.gate.windows import GateState
from kiroku.llm.groq import GroqLLM


async def main(source, out, repeats):
    recorded = [json.loads(line) for line in source.read_text().splitlines()]
    header = recorded[0]["header"]
    split = [r for r in recorded[1:] if r["arm"] == "split" and r["repeat"] == 0]
    settings = Settings.load()
    url = await scratch()
    recorder = Recorder(pace_s=16)
    questions = tuple(
        RuleQuestion(
            r["id"],
            r["name"],
            split[0]["request"]["questions"][f"rule_{r['id']}"]["instructions"],
            Decimal(r["threshold"]),
        )
        for r in header["rules"]
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"header": header}) + "\n")
    async with open_pool(url) as pool, httpx.AsyncClient(transport=recorder) as http:
        gate = LLMGate(GroqLLM(http, settings.secrets.require(SecretName.GROQ)))
        budget = Budget(pool, Caps())
        for repeat in range(repeats):
            for row in split:
                raw = row["request"]["state"]
                state = GateState(raw["context"], raw["new"])
                # Script pacing is outside the 20-second production provider deadline.
                await asyncio.sleep(max(0, recorder.last + 16 - time.monotonic()))
                result = await gate.evaluate(GateRequest(state, questions, budget.guard(None)))
                async with pool.connection() as conn, conn.transaction():
                    for cost in result.costs:
                        await ledger.record(conn, cost)
                scores = {q.name: result.probabilities[q.rule_id] for q in questions}
                with out.open("a") as f:
                    for call in recorder.rows:
                        f.write(
                            json.dumps(
                                {
                                    "repeat": repeat,
                                    "chunk": row["chunk"],
                                    "scores": scores,
                                    "mode": result.mode,
                                    **call,
                                }
                            )
                            + "\n"
                        )
                recorder.rows.clear()
                print(repeat, row["chunk"], scores, result.mode, flush=True)
        async with pool.connection() as conn:
            print(
                "fallback ledger:",
                await (
                    await conn.execute(
                        "SELECT count(*), sum(usd) FROM cost_ledger WHERE purpose = 'gate'"
                    )
                ).fetchone(),
            )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--repeats", type=int, default=3)
    a = p.parse_args()
    asyncio.run(main(a.source, a.out, a.repeats))
