"""The offline name counter must not hide accepted spellings as omissions."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("speech", "observed", "success"),
    [
        ("Timbersaw is here", [""], False),
        ("Timber is here", [""], False),
        ("Nothing here", [""], True),
        ("Timber is here", ["", "Timber"], True),
        ("Timber and Timber", ["", "Timber"], False),
    ],
)
def test_omission_checks_unassigned_reference_and_accepted_spellings(
    tmp_path: Path, speech: str, observed: list[str], success: bool
) -> None:
    audit = {
        "arms": ["title"],
        "arm_scope": {"title": "runtime control"},
        "chunks": [
            {
                "chunk": 0,
                "occurrences": [
                    {
                        "name": "Timbersaw",
                        "accepted": ["Timbersaw", "Timber"],
                        "group": "hero",
                        "observed": {"title": value},
                    }
                    for value in observed
                ],
            }
        ],
    }
    (tmp_path / "audit.json").write_text(json.dumps(audit))
    (tmp_path / "title.jsonl").write_text(
        json.dumps({"start_ms": 0, "end_ms": 1, "text": speech, "usd": "0"}) + "\n"
    )
    (tmp_path / "ledger.json").write_text(json.dumps({"usd": "0", "calls": 0}))
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "spikes/dota_counts.py"), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is success, result.stderr
    if success:
        counts = json.loads((tmp_path / "counts.json").read_text())
        assert counts["arms"]["title"]["omission"] == 1
    else:
        assert "omission contains unassigned accepted spelling" in result.stderr
        assert not (tmp_path / "counts.json").exists()
