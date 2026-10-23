# Local transcription with Fluid Speech Mini

Fluid Speech Mini is an optional English CPU Transcriber. Groq Whisper remains the
default. It uses the same Capture, archive, transcript commits and Backfill as Groq;
it makes no paid transcription calls and needs no ASR API key. Notes, the Gate and
Alerts still use their existing providers and credentials.

Install the optional runtime and checksum-verified, pinned weights:

```sh
uv sync --extra fluid
.venv/bin/kiroku transcriber install-fluid
.venv/bin/kiroku watch caedrel gorgc --transcriber fluid
```

To transcribe without any provider credentials:

```sh
.venv/bin/kiroku watch caedrel gorgc --transcriber fluid --no-gate --no-notes
```

The model files occupy about 302 MB (287 MiB). They live outside the checkout at
`<data_dir>/models/fluid-speech-mini`. Set `KIROKU_FLUID_MODEL_DIR` for another
location; `install-fluid --directory PATH` installs there. Set
`KIROKU_TRANSCRIBER=fluid` to select it for both watch and Backfill without CLI
flags. `KIROKU_FLUID_THREADS` defaults to 2 and accepts 1–32.

A watch shares one model subprocess across all its channels. Inference runs one
chunk at a time; live speech has priority over Backfill. Live PCM retains a bounded
30-minute queued tail plus the current chunk. Evicted speech stays recoverable
from the archive. Backfill applies backpressure and never drops chunks. Shutdown
or a 60-second worker timeout terminates and reaps the subprocess, including hung
native inference. The worker ignores broadcast SIGINT/SIGTERM so the supervisor
can drain speech; closing its stdin ends it after that drain. Separate `kiroku backfill` processes each load their own model;
they do not share memory or priority with another process.

Choose a provider at a session boundary. Transcript spans are keyed by provider
identity. Switching during an existing session leaves the old transcript intact
and automatic Backfill transcribes that session's archives for the new provider,
which can produce duplicate speech in views that show both providers. To explicitly
transcribe an archive with Fluid:

```sh
.venv/bin/kiroku backfill gorgc --session 22 --transcriber fluid
```

## Game terminology and timestamps

This runner has no text prompt, previous-text conditioning or Vocabulary input.
Adding a Dota or League prompt cannot change its predictions. Whisper's prompt is
an inference feature of Whisper, not a general ASR interface. Parakeet can support
contextual phrase biasing with a suitable decoder; this runner currently uses
unbiased greedy TDT decoding. No fine tuning is required to use it. Evaluate real
misspellings before deciding whether to implement phrase biasing or train an
adapted model. Do not feed corrected text back as if it were audio evidence.

Sentence boundaries use punctuation and approximate TDT token emission positions
on the model's 80 ms clock. They are not forced alignment or speaker diarization.
Simultaneous sentence starts are merged to preserve unique transcript offsets.

Replay saved 16 kHz mono signed-16-bit WAV audio through the production worker:

```sh
uv run --extra fluid python spikes/fluid_replay.py --model-dir PATH clip.wav
```

The functional recording in `spikes/results/2026-10-23-fluid-runtime.jsonl`
contains four archived Dota/League clips, input hashes, provider identity and
results. It verifies span coverage, timestamp bounds and absent paid costs; it is
not a human-labelled accuracy score. The synthetic subprocess tests run without
ONNX Runtime or network access.

## Attribution

Weights and the adapted runtime come from
[FluidVoice-Labs/fluid-speech-mini-onnx](https://huggingface.co/FluidVoice-Labs/fluid-speech-mini-onnx),
revision `fc592620325027419883cf272e70588d8a788c0a`. Fluid Speech Mini by FluidVoice
is based on NVIDIA Parakeet and Moondream Parakeet Ultra. The upstream CC BY 4.0
license and NOTICE are included beside the adapted runner. The installer fetches
only pinned data files; it never downloads or executes upstream Python.
