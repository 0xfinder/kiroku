# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Phase 0 spike: live Twitch audio -> 30 s chunks -> Groq Whisper -> Jev gate.

Usage: uv run spikes/live_gate.py <channel> <minutes> <outdir>
Reads GROQ_API_KEY and JEV_API_KEY from the environment.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

CHUNK_S = 30
RULES = {
    "predicts_winner": "In `window`, does the speaker predict which team will win the current game or series?",
    "draft_opinion": "In `window`, does the speaker give an opinion on a team's draft or hero picks?",
    "talks_item": "In `window`, does the speaker discuss a specific item purchase or item timing?",
}
MODE = {
    "type": "choice",
    "instructions": "What is the streamer doing during `window`, given `stream`?",
    "criteria": {
        "playing": "The streamer is playing a match himself",
        "watching_pro": "The streamer is watching and commentating a professional match",
        "chatting": "Talking with chat, no game being discussed",
        "other": None,
    },
}


def stream_title(channel: str) -> dict:
    out = subprocess.run(
        ["uvx", "streamlink", "--json", f"twitch.tv/{channel}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["metadata"] or {}


def transcribe(client: httpx.Client, path: Path, prompt: str) -> tuple[str, float]:
    t = time.monotonic()
    r = client.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
        files={"file": (path.name, path.read_bytes(), "audio/ogg")},
        data={"model": "whisper-large-v3-turbo", "language": "en", "prompt": prompt},
    )
    r.raise_for_status()
    return r.json()["text"].strip(), time.monotonic() - t


def gate(client: httpx.Client, meta: dict, window: str) -> tuple[dict, float, int]:
    questions = {"mode": MODE} | {k: {"type": "noul", "instructions": v} for k, v in RULES.items()}
    t = time.monotonic()
    r = client.post(
        "https://api.typesafe.ai/v1/systemone",
        headers={"Authorization": f"Bearer {os.environ['JEV_API_KEY']}"},
        json={"model": "jev-latest", "state": {"stream": meta, "window": window}, "questions": questions},
    )
    r.raise_for_status()
    body = r.json()
    return body["answers"], time.monotonic() - t, body["usage"]["input_tokens"]


def main(channel: str, minutes: float, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    meta = stream_title(channel)
    vocab = f"{meta.get('category', '')} stream. {meta.get('title', '')}."
    capture = subprocess.Popen(
        f"uvx streamlink --stdout twitch.tv/{channel} audio_only 2>/dev/null | "
        f"ffmpeg -loglevel error -i pipe:0 -vn -ac 1 -ar 16000 -c:a libopus -b:a 32k "
        f"-f segment -segment_time {CHUNK_S} -reset_timestamps 1 {outdir}/chunk_%04d.ogg",
        shell=True,
    )
    deadline = time.monotonic() + minutes * 60
    log = (outdir / "log.jsonl").open("w")
    done, prev = 0, ""
    with httpx.Client(timeout=30) as client:
        while time.monotonic() < deadline:
            # A segment is complete once the next one exists.
            if not (outdir / f"chunk_{done + 1:04d}.ogg").exists():
                time.sleep(1)
                continue
            path = outdir / f"chunk_{done:04d}.ogg"
            text, stt_s = transcribe(client, path, f"{vocab} {prev[-400:]}")
            window = f"{prev[-300:]} {text}".strip()
            answers, gate_s, tokens = gate(client, meta, window)
            row = {"chunk": done, "text": text, "stt_s": round(stt_s, 2), "gate_s": round(gate_s, 2),
                   "jev_tokens": tokens, "mode": answers["mode"]["choice"],
                   "rules": {k: answers[k]["noul"] for k in RULES}}
            log.write(json.dumps(row) + "\n")
            log.flush()
            print(json.dumps(row), flush=True)
            prev, done = text, done + 1
    capture.terminate()
    subprocess.run(["pkill", "-f", f"twitch.tv/{channel}"], check=False)


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]), Path(sys.argv[3]))
