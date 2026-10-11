"""Issue #3 acceptance, live: does the context/new split stop spike finding 2?

Replays the 2026-10-11 Gorgc spike's eight 30 s chunks through the production Gate
(kiroku.gate.jev.JevGate, kiroku.gate.windows.build_state, kiroku.gate.rule_question), one
Window per chunk, in three state layouts:

  split     the production layout: {"context": {"stream", "earlier"}, "new"}
  merged    the spike's control: one `window` field holding the previous tail + the chunk
  new_only  split without `context.earlier`

Usage: uv run python spikes/gate_split.py <spike.jsonl> <out.jsonl> [--repeats N]

Writes a header line (Rules, model, policy) and one line per call with the exact request and
response bodies. tests/test_gate_replay.py replays the `split` arm offline, and fails if the
production request changes without re-running this. Each call writes a Cost ledger row
(purpose rule_test) to KIROKU_DATABASE_URL. Exits 1 unless `split` passes predicts_winner on
chunks 0 and 7 and not on chunk 1 in every repeat.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from kiroku import ledger
from kiroku.budget import Unlimited
from kiroku.config import SecretName, Settings
from kiroku.db import connect
from kiroku.gate import MODE_CRITERIA, GateRequest, RuleQuestion, rule_question
from kiroku.gate.jev import DEFAULT_MODEL, JevGate, usd
from kiroku.gate.windows import Bounds, GateState, Turn, WindowPolicy, build_state
from kiroku.ledger import Purpose
from kiroku.rules import Rule

CHUNK_MS = 30_000
POLICY = WindowPolicy(length_ms=CHUNK_MS)
# The spike's metadata, as streamlink reported it for the co-stream.
STREAM = {"category": "Dota 2", "title": "BLAST Slam VIII co-stream: Spirit vs PARIVISION"}
# The spike's three Rules as conditions. Thresholds were fixed before the first live run.
RULES = [
    Rule(1, "predicts_winner", "Does the speaker predict which team will win the current game "
         "or series?", Decimal("0.60"), None, 600),
    Rule(2, "draft_opinion", "Does the speaker give an opinion on a team's draft or hero picks?",
         Decimal("0.70"), None, 600),
    Rule(3, "talks_item", "Does the speaker discuss a specific item purchase or item timing?",
         Decimal("0.70"), None, 600),
]
SPIKE_RULES = {  # the spike's own wording, for the merged control
    "predicts_winner": "In `window`, does the speaker predict which team will win the current "
    "game or series?",
    "draft_opinion": "In `window`, does the speaker give an opinion on a team's draft or hero "
    "picks?",
    "talks_item": "In `window`, does the speaker discuss a specific item purchase or item timing?",
}
EXPECT = {0: True, 1: False, 7: True}  # issue #3: predicts_winner per chunk


def split_state(turns: list[Turn], i: int, *, earlier: bool) -> GateState:
    bounds = Bounds(i * CHUNK_MS, (i + 1) * CHUNK_MS)
    state = build_state(bounds, turns, STREAM, POLICY)
    if earlier:
        return state
    return GateState({"stream": state.context["stream"]}, state.new)


def merged_body(texts: list[str], i: int) -> dict[str, Any]:
    prev = texts[i - 1] if i else ""
    return {
        "model": DEFAULT_MODEL,
        "state": {"stream": STREAM, "window": f"{prev[-300:]} {texts[i]}".strip()},
        "questions": {
            "mode": {
                "type": "choice",
                "instructions": "What is the streamer doing during `window`, given `stream`?",
                "criteria": {m.value: d for m, d in MODE_CRITERIA.items()},
            },
            **{f"rule_{r.id}": {"type": "noul", "instructions": SPIKE_RULES[r.name]}
               for r in RULES},
        },
    }


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.last: tuple[Any, Any] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        self.last = (json.loads(request.content), json.loads(body))
        return response


async def main(spike: Path, out: Path, repeats: int) -> int:
    settings = Settings.load()
    key = settings.secrets.require(SecretName.JEV)
    texts = [json.loads(line)["text"] for line in spike.read_text().splitlines()]
    turns = [Turn(i * CHUNK_MS, t) for i, t in enumerate(texts)]
    questions = [rule_question(r) for r in RULES]
    recorder = Recorder()
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=recorder) as http, connect(settings.database_url) as db:
        gate = JevGate(http, key)
        for repeat in range(repeats):
            for i in range(len(texts)):
                for arm in ("split", "merged", "new_only"):
                    if arm == "merged":
                        raw = (await http.post(
                            "https://api.typesafe.ai/v1/systemone",
                            headers={"Authorization": f"Bearer {key.reveal()}"},
                            json=merged_body(texts, i),
                        )).json()
                        scores = {r.name: raw["answers"][f"rule_{r.id}"]["noul"] for r in RULES}
                        mode, tokens = raw["answers"]["mode"]["choice"], raw["usage"]["input_tokens"]
                        await ledger.record(db, ledger.CostEntry(
                            "jev", raw["model"], Purpose.RULE_TEST, ledger.Tokens(tokens),
                            usd(tokens), 0))
                    else:
                        state = split_state(turns, i, earlier=arm == "split")
                        answer = await gate.evaluate(GateRequest(
                            state, tuple(questions), Unlimited(), Purpose.RULE_TEST))
                        for cost in answer.costs:
                            await ledger.record(db, cost)
                        scores = {q.name: answer.probabilities[q.rule_id] for q in questions}
                        mode, tokens = answer.mode, answer.costs[0].usage
                    assert recorder.last is not None
                    request, response = recorder.last
                    rows.append({"arm": arm, "repeat": repeat, "chunk": i, "scores": scores,
                                 "mode": str(mode), "request": request, "response": response})
                    print(f"r{repeat} chunk {i} {arm:8} " + " ".join(
                        f"{k}={v:.2f}" for k, v in scores.items()), file=sys.stderr)

    header = {"model": DEFAULT_MODEL, "stream": STREAM, "chunk_ms": CHUNK_MS,
              "policy": dataclasses.asdict(POLICY),
              "rules": [{"id": r.id, "name": r.name, "instructions": r.instructions,
                         "threshold": str(r.threshold)} for r in RULES]}
    with out.open("w") as f:
        f.write(json.dumps({"header": header}) + "\n")
        for row in rows:
            f.write(json.dumps(row) + "\n")

    print("\npredicts_winner (min-max over repeats), threshold 0.60")
    print("chunk  split      merged     new_only")
    ok = True
    for i in range(len(texts)):
        cells = []
        for arm in ("split", "merged", "new_only"):
            ps = [r["scores"]["predicts_winner"] for r in rows if r["arm"] == arm and r["chunk"] == i]
            cells.append(f"{min(ps):.2f}-{max(ps):.2f}")
            if arm == "split" and i in EXPECT:
                ok &= all((p >= 0.60) == EXPECT[i] for p in ps)
        print(f"{i:5}  " + "  ".join(f"{c:9}" for c in cells))
    print("\nsplit: chunks 0 and 7 pass, chunk 1 doesn't:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("spike", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--repeats", type=int, default=3)
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.spike, a.out, a.repeats)))
