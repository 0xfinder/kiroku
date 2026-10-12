"""Recount the reading-only name audit. No API calls or audio needed.

uv run python spikes/league_counts.py spikes/results/2026-10-12-league-vocabulary
"""
import collections
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
audit = json.loads((path / "audit.json").read_text())
reference = sum(len(chunk["reference"]) for chunk in audit["chunks"])
counts = {side: collections.Counter() for side in ("title", "pack")}
for side, totals in counts.items():
    rows = [json.loads(s) for s in (path / f"{side}.jsonl").read_text().splitlines()]
    for chunk in audit["chunks"]:
        text = rows[chunk["chunk"]]["text"].casefold()
        occurrences = collections.Counter(e["observed"].casefold() for e in chunk[side] if e["observed"])
        for observed, count in occurrences.items():
            assert text.count(observed) >= count, (side, chunk["chunk"], observed)
        for error in chunk[side]:
            assert error["name"] in chunk["reference"]
            group = "champion" if error["name"] in audit["champions"] else "player"
            totals[group] += 1
            totals[error["kind"]] += 1
    print(side, dict(totals), "total", totals["champion"] + totals["player"], "reference", reference)
print("Reading-only reference, not audio-grounded WER. See audit.json for exclusions and every occurrence.")
