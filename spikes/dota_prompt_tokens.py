"""Count actual Whisper prompt tokens (optional spike dependency, not runtime).

uv run --with tiktoken python spikes/dota_prompt_tokens.py <outdir>
Uses OpenAI Whisper's multilingual ranks; no credentials or paid API.
"""

import base64
import json
import sys
from pathlib import Path

import httpx
import tiktoken

URL = "https://raw.githubusercontent.com/openai/whisper/main/whisper/assets/multilingual.tiktoken"
response = httpx.get(URL)
response.raise_for_status()
ranks = {
    base64.b64decode(token): int(rank)
    for token, rank in (line.split() for line in response.content.splitlines())
}
encoding = tiktoken.Encoding(
    name="whisper",
    pat_str=r"'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+",
    mergeable_ranks=ranks,
    special_tokens={},
)
out = Path(sys.argv[1])
counts = {}
for arm in json.loads((out / "audit.json").read_text())["arms"]:
    rows = [json.loads(s) for s in (out / f"{arm}.jsonl").read_text().splitlines()]
    counts[arm] = [len(encoding.encode(r["prompt"])) for r in rows]
    print(arm, min(counts[arm]), max(counts[arm]))
(out / "prompt-tokens.json").write_text(
    json.dumps({"ranks": URL, "counts": counts}, indent=2) + "\n"
)
