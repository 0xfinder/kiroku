"""A stand-in for the streamlink executable, driven by environment variables.

FAKE_STREAM_ID   the broadcast id `--json` reports (unset: offline)
FAKE_LEGS        comma-separated leg lengths in seconds, e.g. "40,30"
FAKE_STATE_DIR   where the leg counter lives, so each `--stdout` run plays the next leg
FAKE_HOLD        if set, a leg streams at FAKE_PACE x real time, then stays open, like a
                 live stream mid-broadcast (so audio is always in flight when it stops),
                 until stopped or until its parent dies (real streamlink then gets EPIPE)

`--stdout` writes the next leg as WAV and exits, which is what a stream drop looks like to
the pipeline. With no legs left it exits 1 without output, like an offline stream.
"""

import json
import os
import sys
import time
from pathlib import Path

from speech import speechlike, wav  # same directory: this file runs as a script

args = sys.argv[1:]
if "--json" in args:
    stream_id = os.environ.get("FAKE_STREAM_ID")
    if stream_id:
        print(
            json.dumps(
                {
                    "metadata": {
                        "id": stream_id,
                        "author": "Fake",
                        "category": os.environ.get("FAKE_CATEGORY", "Dota 2"),
                        "title": os.environ.get("FAKE_TITLE", "fake stream"),
                    },
                    "streams": {"audio_only": {}},
                }
            )
        )
    else:
        print(json.dumps({"error": "No playable streams found on this URL: twitch.tv/x"}))
        sys.exit(1)
    sys.exit(0)

state = Path(os.environ["FAKE_STATE_DIR"]) / "legs_played"
played = int(state.read_text()) if state.exists() else 0
legs = [float(x) for x in os.environ.get("FAKE_LEGS", "").split(",") if x]
if played >= len(legs):
    sys.exit(1)
state.write_text(str(played + 1))
pcm, _ = speechlike(legs[played], seed=played)
data = wav(pcm)
if not os.environ.get("FAKE_HOLD"):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    sys.exit(0)
parent = os.getppid()
pace = float(os.environ.get("FAKE_PACE", "10"))
piece = 3_200  # 0.1 s of audio
for i in range(0, len(data), piece):
    sys.stdout.buffer.write(data[i : i + piece])
    sys.stdout.buffer.flush()
    time.sleep(0.1 / pace)
while os.getppid() == parent:
    time.sleep(0.1)
