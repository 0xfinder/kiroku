"""Issue #4 acceptance, live: does `rule test` separate a vague Rule from a reworded one?

Runs the production Rule test path (kiroku.dry_run.run) over the spike's eight Windows, using
the exact state the Gate saw (the `split` arm of 2026-10-11-gate-split.jsonl), for the spike's
`draft_opinion` wording and a reworded one. That's the same requests `kiroku rule test
draft_opinion` sends with and without `--instructions`.

Usage: KIROKU_DATABASE_URL=<migrated db> uv run python spikes/rule_wording.py <out.jsonl>

Writes one line per call with the exact request and response; tests/test_rule_cli.py replays
them. Each call writes a Cost ledger row (purpose rule_test) to KIROKU_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from kiroku import dry_run
from kiroku.budget import Budget
from kiroku.config import SecretName, Settings
from kiroku.db import open_pool
from kiroku.gate import rule_question
from kiroku.gate.jev import JevGate
from kiroku.gate.windows import GateState
from kiroku.rules import Rule

SPLIT = Path("spikes/results/2026-10-11-gate-split.jsonl")
OLD = "Does the speaker give an opinion on a team's draft or hero picks?"
NEW = (
    "Does the speaker talk about picking or banning heroes in this game's draft, such as why "
    "a hero was picked, banned, or should have been picked?"
)
# The saved Rule is the only one in a fresh database (id 1); a reworded trial is unsaved (id 0).
WORDINGS = {"old": Rule(1, "draft_opinion", OLD, Decimal("0.7"), None, 600),
            "new": Rule(0, "draft_opinion", NEW, Decimal("0.7"), None, 0)}


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.calls: list[tuple[Any, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        self.calls.append((json.loads(request.content), json.loads(body)))
        return response


async def main(out: Path) -> None:
    settings = Settings.load()
    key = settings.secrets.require(SecretName.JEV)
    rows = [json.loads(line) for line in SPLIT.read_text().splitlines()][1:]
    split = sorted((r for r in rows if r["arm"] == "split" and r["repeat"] == 0),
                   key=lambda r: r["chunk"])
    windows = [
        dry_run.StoredWindow(r["chunk"], 1, r["chunk"] * 30_000,
                             GateState(r["request"]["state"]["context"], r["request"]["state"]["new"]))
        for r in split
    ]
    recorder = Recorder()
    lines: list[dict[str, Any]] = []
    async with open_pool(settings.database_url) as pool, httpx.AsyncClient(transport=recorder) as http:
        guard = Budget(pool, settings.caps).guard(None)
        for label, rule in WORDINGS.items():
            question = rule_question(rule)
            trials = await dry_run.run(pool, JevGate(http, key), guard, question, windows)
            print(f"{label}: " + " ".join(
                f"{t.probability:.2f}" if t.probability is not None else "-" for t in trials))
            print(dry_run.report(rule.name, question, trials), "\n", file=sys.stderr)
    for request, response in recorder.calls:
        lines.append({"request": request, "response": response})
    out.write_text("".join(json.dumps(line) + "\n" for line in lines))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
