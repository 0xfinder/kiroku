"""Validate and recount every annotation in the reading-only Dota name audit.

uv run python spikes/dota_counts.py spikes/results/2026-10-14-dota-vocabulary
This makes no API calls. Reference spellings/exclusions live in audit.json.
"""

import collections
import json
import re
import sys
from decimal import Decimal
from pathlib import Path


def count(path):
    audit = json.loads((path / "audit.json").read_text())
    arms = tuple(audit["arms"])
    rows = {
        a: [json.loads(s) for s in (path / f"{a}.jsonl").read_text().splitlines()]
        for a in arms
    }
    boundaries = {
        a: [(r["start_ms"], r["end_ms"]) for r in rs] for a, rs in rows.items()
    }
    assert all(b == boundaries["title"] for b in boundaries.values())
    counts = {a: collections.Counter() for a in arms}
    occurrences = 0
    for chunk in audit["chunks"]:
        used = {a: collections.Counter() for a in arms}
        assigned = {
            arm: collections.Counter(
                o["observed"][arm].casefold()
                for o in chunk["occurrences"] if o["observed"][arm]
            ) for arm in arms
        }
        for occurrence in chunk["occurrences"]:
            occurrences += 1
            assert occurrence["group"] in ("player", "hero")
            for arm in arms:
                observed = occurrence["observed"][arm]
                text = rows[arm][chunk["chunk"]]["text"]
                if not observed:
                    for spelling in {occurrence["name"], *occurrence.get("accepted", [])}:
                        # Consume surviving repeated names once across the chunk:
                        # the second Malady in chunk 13 and first Timber in chunk 19
                        # can be omitted while another reference keeps that spelling.
                        matches = re.findall(
                            rf"(?<!\w){re.escape(spelling)}(?!\w)", text, re.I
                        )
                        assert len(matches) <= assigned[arm][spelling.casefold()], (
                            "omission contains unassigned accepted spelling",
                            chunk["chunk"], arm, spelling,
                        )
                if observed:
                    used[arm][observed.casefold()] += 1
                    matches = re.findall(
                        rf"(?<!\w){re.escape(observed)}(?!\w)", text, re.I
                    )
                    assert len(matches) >= used[arm][observed.casefold()], (
                        chunk["chunk"],
                        arm,
                        observed,
                    )
                counts[arm]["reference"] += 1
                if observed.casefold() not in {
                    s.casefold()
                    for s in occurrence.get("accepted", [occurrence["name"]])
                }:
                    counts[arm][occurrence["group"]] += 1
                    counts[arm]["omission" if not observed else "misspelling"] += 1
                    counts[arm]["errors"] += 1
    for arm in arms:
        counts[arm]["usd"] = str(sum(Decimal(r["usd"]) for r in rows[arm]))
        counts[arm]["calls"] = sum(Decimal(r["usd"]) > 0 for r in rows[arm])
    total = sum(Decimal(c["usd"]) for c in counts.values())
    ledger = json.loads((path / "ledger.json").read_text())
    assert Decimal(ledger["usd"]) == total
    assert ledger["calls"] == sum(c["calls"] for c in counts.values())
    result = {
        "reference_occurrences": occurrences,
        "scope": audit["arm_scope"],
        "arms": counts,
        "usd": str(total),
    }
    print(json.dumps(result, indent=2))
    print(
        "Reading-only name audit, not audio-grounded WER. See audit.json for exclusions."
    )
    return result


if __name__ == "__main__":
    path = Path(sys.argv[1])
    result = count(path)
    (path / "counts.json").write_text(json.dumps(result, indent=2) + "\n")
