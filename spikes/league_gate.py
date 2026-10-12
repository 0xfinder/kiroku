"""Record Jev's Gate state with League live context, billed through the provider.

KIROKU_DATABASE_URL=<migrated spike db> uv run python spikes/league_gate.py <out.jsonl>
The match is completed. These calls validate the state layout on recorded speech.
"""

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx

from kiroku import ledger
from kiroku.budget import Budget
from kiroku.config import SecretName, Settings
from kiroku.db import open_pool
from kiroku.gate import GateRequest, rule_question
from kiroku.gate.jev import JevGate
from kiroku.gate.windows import Bounds, GateState, Turn, WindowPolicy, build_state
from kiroku.ledger import Purpose
from kiroku.packs.league.data import match_context, names
from kiroku.rules import Rule

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/league"
TRANSCRIPTS = Path(__file__).parent / "results/2026-10-12-league-vocabulary/pack.jsonl"
STREAM = {"category": "League of Legends", "title": "DEMACIA CUP FLY VS RED - LAST SPOTS IN THE TOP 8"}
RULES = [
    Rule(1, "yunara_broken", "Does the speaker say Yunara is broken or overpowered?", Decimal("0.70"), "league", 600),
    Rule(2, "draft", "Does the speaker talk about picking or banning champions in this game's draft, such as why a champion was picked, banned, or should have been picked?", Decimal("0.70"), "league", 600),
]


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()
        self.last = None

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        self.last = (json.loads(request.content), json.loads(body))
        return response

    async def aclose(self):
        await self.inner.aclose()


async def run(out):
    settings = Settings.load()
    event = json.loads((FIXTURES / "getEventDetails.json").read_text())["data"]["event"]
    event["match"]["games"][0]["state"] = "inProgress"
    summary, key, _ = match_context(event, json.loads((FIXTURES / "window.json").read_text()), names(json.loads((FIXTURES / "champion.json").read_text())))
    rows = [json.loads(s) for s in TRANSCRIPTS.read_text().splitlines()]
    texts = [rows[9]["text"], rows[27]["text"]]
    questions = tuple(rule_question(r) for r in RULES)
    recorder = Recorder()
    async with open_pool(settings.database_url) as pool, httpx.AsyncClient(transport=recorder) as http:
        gate = JevGate(http, settings.secrets.require(SecretName.JEV))
        with out.open("w") as f:
            f.write(json.dumps({"header": {"stream": STREAM, "live": summary, "game_key": key,
                "texts": texts, "rules": [{"id": r.id, "name": r.name, "instructions": r.instructions,
                    "threshold": str(r.threshold)} for r in RULES]}}) + "\n")
            for repeat in range(3):
                for index, text in enumerate(texts):
                    for arm in ("no_live", "live"):
                        state = build_state(Bounds(index*45000, (index+1)*45000),
                            [Turn(i*45000, t) for i,t in enumerate(texts)], STREAM, WindowPolicy())
                        if arm == "live":
                            state = GateState({**state.context, "live": summary}, state.new)
                        answer = await gate.evaluate(GateRequest(state, questions, Budget(pool, settings.caps).guard(None), Purpose.RULE_TEST))
                        async with pool.connection() as conn:
                            for cost in answer.costs:
                                await ledger.record(conn, cost)
                        request, response = recorder.last
                        row = {"repeat": repeat, "index": index, "arm": arm, "request": request,
                            "response": response, "mode": str(answer.mode),
                            "scores": {q.name: answer.probabilities[q.rule_id] for q in questions}}
                        f.write(json.dumps(row)+"\n"); f.flush()
                        print(repeat, index, arm, row["scores"], row["mode"], flush=True)


asyncio.run(run(Path(sys.argv[1])))
