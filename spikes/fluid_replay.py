"""Replay 16 kHz mono PCM WAVs through the production Fluid worker, without API calls.

uv run --extra fluid python spikes/fluid_replay.py --model-dir PATH clip.wav ...
"""
import argparse
import asyncio
import dataclasses
import hashlib
import json
import wave
from pathlib import Path

from kiroku.audio import AudioBlock
from kiroku.budget import Unlimited
from kiroku.transcriber import Context
from kiroku.transcriber.fluid import FluidTranscriber
from kiroku.transcriber.fluid_assets import verify


async def replay(directory: Path, files: list[Path]) -> None:
    await asyncio.to_thread(verify, directory)
    transcriber = FluidTranscriber.local(directory, 2)
    try:
        await transcriber.start()
        for path in files:
            with wave.open(str(path)) as audio:
                assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
                pcm = audio.readframes(audio.getnframes())

            async def events():
                yield AudioBlock(0, pcm)

            results = [r async for r in transcriber.transcribe(events(), Context(lambda: "", Unlimited()))]
            assert all(r.cost is None for r in results)
            assert results[0].start_ms == 0
            assert results[-1].end_ms == len(pcm) // 32
            assert all(0 <= u.start_ms < u.end_ms <= r.end_ms for r in results for u in r.utterances)
            print(json.dumps({"input": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "transcriber": transcriber.name, "results": [dataclasses.asdict(r) for r in results]}), flush=True)
    finally:
        await transcriber.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args()
    asyncio.run(replay(args.model_dir, args.files))
