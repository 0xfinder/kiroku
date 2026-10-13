"""Recount issue #7 recording validity, timestamp bounds, team and prediction fields, and cost."""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from kiroku.gate.windows import Bounds
from kiroku.ledger import Tokens
from kiroku.llm.groq import PRICE
from kiroku.notes_schema import GENERAL
from kiroku.packs.league.notes import SCHEMAS


def timestamps(value):
    if isinstance(value, dict):
        if "at_ms" in value:
            yield value["at_ms"]
        for child in value.values():
            yield from timestamps(child)
    elif isinstance(value, list):
        for child in value:
            yield from timestamps(child)


def main(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    calls = [r for r in rows if "request" in r]
    results = [r["result"] for r in rows if "result" in r]
    entries = [n for r in results for n in r["notes"]]
    ok = [n for n in entries if n["status"] == "ok"]
    for note in ok:
        schema = SCHEMAS["watching_pro"] if note["schema_name"] == "league.watching_pro" else GENERAL
        schema.parse(note["body"], Bounds(note["start_ms"], note["end_ms"]))
    stamps = [(n, at) for n in ok for at in timestamps(n["body"])]
    league = [n for n in ok if n["schema_name"] == "league.watching_pro"]
    usage_cost = sum((PRICE.usd(Tokens(r["response"]["usage"]["prompt_tokens"],
                                      r["response"]["usage"]["completion_tokens"]))
                      for r in calls if r["status"] == 200), Decimal(0))
    ledger_cost = sum((Decimal(r["ledger"][1]) for r in results), Decimal(0))
    report = {
        "requests": len(calls), "notes": len(entries), "ok": len(ok),
        "timestamps": len(stamps),
        "timestamps_in_span": sum(n["start_ms"] <= at < n["end_ms"] for n, at in stamps),
        "nonempty_summaries": sum(bool(n["body"]["summary"]) for n in ok),
        "league_notes": len(league),
        "league_team_pairs": sum(n["body"]["teams"] == ["RED Kalunga", "FlyQuest"] for n in league),
        "league_prediction_items": sum(len(n["body"]["predictions"]) for n in league),
        "favorite_takes_at_518940": sum(t["at_ms"] == 518_940 for n in league for t in n["body"]["takes"]),
        "response_usage_usd": str(usage_cost), "ledger_usd": str(ledger_cost),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("recording", type=Path)
    main(p.parse_args().recording)
