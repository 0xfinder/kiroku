"""Issue #5: production Extraction/Novelty, three repeats of two Cooldown arms."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from alerts_common import Recorder, scratch, seed

from kiroku import alerting
from kiroku.budget import Budget, Caps
from kiroku.config import SecretName, Settings
from kiroku.db import open_pool
from kiroku.llm.groq import GroqLLM
from kiroku.notify import Delivery


class Sink:
    name = "telegram"

    async def send(self, message, *, give_up_s):
        return Delivery(f"<CHAT_ID>:{message.alert_id}")

    async def taps(self):
        if False:
            yield

    async def confirm(self, tap, *, recorded):
        pass


async def main(source, out, repeats):
    settings = Settings.load()
    url = await scratch("kiroku_spike_5_extraction_v3")
    recorder = Recorder(pace_s=16)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "header": {
                    "model": "openai/gpt-oss-120b",
                    "repeats": repeats,
                    "database": "kiroku_spike_5_extraction_v3",
                    "pace_s": 16,
                }
            }
        )
        + "\n"
    )
    async with open_pool(url) as pool, httpx.AsyncClient(transport=recorder) as http:
        llm = GroqLLM(http, settings.secrets.require(SecretName.GROQ))
        budget = Budget(pool, Caps())
        epoch = datetime.now(UTC)
        async with pool.connection() as conn:
            last = await (
                await conn.execute("SELECT max(started_at) FROM stream_session")
            ).fetchone()
        if last and last[0] is not None:
            epoch = max(epoch, last[0] + timedelta(days=1))
        for repeat in range(repeats):
            for index, arm in enumerate(("default", "novelty_only")):
                # Separate replay days keep cross-arm Cooldowns independent in one scratch DB.
                now = epoch + timedelta(days=repeat * 2 + index)
                channel, session, now = await seed(
                    pool, url, cooldown=600 if arm == "default" else 0, now=now, source=source
                )
                worker = alerting.AlertWorker(
                    pool, [channel.id], llm, Sink(), budget, now=lambda now=now: now
                )
                await worker.drain()
                async with pool.connection() as conn:
                    rows = await (
                        await conn.execute(
                            """SELECT r.name, w.start_ms / 30000, a.outcome,
                                      a.content, a.previous_alert_id
                               FROM alert a JOIN gate_decision d ON d.id = a.decision_id
                               JOIN gate_window w ON w.id = d.window_id
                               JOIN rule r ON r.id = a.rule_id WHERE a.session_id = %s
                               ORDER BY w.start_ms, r.id""",
                            [session],
                        )
                    ).fetchall()
                with out.open("a") as f:
                    for call in recorder.rows:
                        f.write(json.dumps({"arm": arm, "repeat": repeat, **call}) + "\n")
                    f.write(
                        json.dumps(
                            {"result": {"arm": arm, "repeat": repeat, "alerts": rows}}, default=str
                        )
                        + "\n"
                    )
                recorder.rows.clear()
                print(arm, repeat, [(r[0], r[1], r[2]) for r in rows], flush=True)
        async with pool.connection() as conn:
            print(
                "extraction ledger:",
                await (
                    await conn.execute(
                        "SELECT count(*), sum(usd) FROM cost_ledger WHERE purpose = 'extraction'"
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
