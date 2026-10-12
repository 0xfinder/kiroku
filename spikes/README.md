# Spikes

Short experiments that decide design questions with evidence. Each one records what was run, what was measured, and what it changed in the design.

## 2026-10-11: Groq Whisper + Jev gate on a live co-stream

**Setup:** `uv run spikes/live_gate.py gorgc 4.5 <outdir>` during Gorgc's official co-stream of Blast Slam VIII, Spirit vs PARIVISION. The pipeline: streamlink `audio_only` → ffmpeg 30 s Opus segments → Groq `whisper-large-v3-turbo` (prompt: stream title + the previous chunk's tail) → one Jev request per chunk with Mode plus three Rules. Output is in [results/2026-10-11-gorgc-blast-slam.jsonl](results/2026-10-11-gorgc-blast-slam.jsonl): 8 chunks.

**Measurements:**

| | Result |
| --- | --- |
| Capture cost on this host | 9% of one CPU, 65 MB RSS |
| Groq transcription latency | 0.40–0.72 s per 30 s chunk |
| Jev latency | 0.24–0.33 s per request (Mode + 3 Rules) |
| Jev input | about 700 tokens per window, so about $0.02 for a 6-hour stream |
| Groq free tier (from response headers) | 7,200 audio seconds and 2,000 requests per rolling window |
| End-to-end alert latency | dominated by the 30 s chunk length; processing adds about 1 s |

**Findings:**

1. **Mode was correct on every chunk:** `watching_pro` at 1.0.
2. **`predicts_winner` caught both real predictions:** "69% for Spirit, I agree, it's like a 70-30 game" scored 0.70, and "there's no way you can fight… I think it's too much" scored 0.72. **It also re-fired at 0.70 on the next chunk**, which contained no prediction. The window carried the previous chunk's tail, and Jev judged the window as a whole. → The Gate needs separate `context` and `new` fields, with Rules asking about `new` only. Context should inform the answer, not trigger it. This refines D4 and D14.
3. **`draft_opinion` scored 0.81–0.98 on every chunk.** Commentary on a pro game mentions heroes constantly, and Jev reads instructions literally. Rule wording matters as much as the threshold. → The Rule editor needs a "test this Rule against recent windows" button. Labels (D15) are needed from day one, not as a later improvement.
4. **Transcription errors come almost entirely from game terms:** "Yatora"/"Ytoro" (Yatoro), "AIM"/"Anthi-Mage" (AM/Anti-Mage), "scotty" (Skadi), "Amber" (Ember). The stream title alone is not enough biasing. → Pack Vocabulary must come from real data: the hero and item constants plus the current rosters (live match data in the League Pack).
5. **Casters and the streamer are mixed in the transcript**, e.g. official play-by-play ("The Toro continues to clean up the waves") next to Gorgc reading chat ("Okay, don't say that"). This confirms Muse's speaker labels are needed for co-streams (D3, D13).
6. **Fixed 30 s cuts split sentences** ("…Disruption…"). Muse streaming avoids this. For the Groq Fallback, cut on silence instead of fixed time, or overlap chunks.

## 2026-10-11: Context/new split for the Gate (issue #3)

**Setup:** `uv run python spikes/gate_split.py spikes/results/2026-10-11-gorgc-blast-slam.jsonl spikes/results/2026-10-11-gate-split.jsonl --repeats 3`. The script replays the spike's eight 30 s chunks through the production Gate (`JevGate`, `build_state`, `rule_question`), pinned to `jev-1.13.0`, one Window per chunk. It runs each chunk three times in three state layouts:

- **split:** production. `{"context": {"stream", "earlier"}, "new"}`, and every Rule wrapped in "judge only `new`".
- **merged:** the spike's original layout. One `window` field holding the previous chunk's 300-character tail plus the chunk, with the spike's wording.
- **new_only:** split without `context.earlier`.

The thresholds were fixed before the run: `predicts_winner` at 0.60, the others at 0.70. The output holds every request and response; [results/2026-10-11-gate-split.jsonl](results/2026-10-11-gate-split.jsonl). The numbers below are from the second recording, made after a review fix changed how `context.earlier` is trimmed. The first recording gave the same picture within 0.04.

**`predicts_winner`, min–max over 3 repeats:**

| Chunk | split | merged | new_only | What was said |
| --- | --- | --- | --- | --- |
| 0 | **0.68–0.69** | 0.68–0.72 | 0.65–0.72 | "69% for Spirit, I agree, it's like a 70-30 game" |
| 1 | **0.10–0.13** | 0.68–0.69 | 0.10–0.11 | no prediction; chunk 0's call is in its context |
| 2 | 0.06 | 0.12 | 0.05 | |
| 3 | 0.11–0.12 | 0.16–0.18 | 0.10 | |
| 4 | 0.24–0.27 | 0.35–0.37 | 0.28–0.31 | "how can they win? ... I think they can still" |
| 5 | 0.11–0.12 | 0.19–0.21 | 0.15–0.16 | |
| 6 | 0.08–0.09 | 0.16 | 0.08–0.10 | |
| 7 | **0.66–0.68** | 0.66–0.67 | 0.61–0.65 | "There's no way you can fight... I think it's too much" |

**Findings:**

1. **The split fixes spike finding 2.** Chunk 1 fell from 0.68–0.69 to 0.10–0.13, while both real predictions still pass in every repeat. The merged control reproduces the original false positive, so this is the layout's effect and not model drift.
2. **Context helps where it should.** Chunk 7's prediction is implicit, and it scores 0.66–0.68 with `earlier` against 0.61–0.65 without it. Dropping context would leave it close to the threshold.
3. **The split also sharpens other Rules.** `draft_opinion` on chunk 3 (play-by-play, no draft talk) fell from 0.91 (merged) to 0.05, and on chunk 7 from 0.80–0.84 to 0.40–0.44. It still scores 0.85–0.96 on chunks that only mention heroes, so finding 3 (Rule wording) stands; that is #4's `rule test`.
4. **Jev is nearly deterministic.** Repeats differ by at most 0.05, and the response reports the model version (`jev-1.13.0`).
5. **Size and cost.** 770–926 input tokens per Window with three Rules, about $0.00004. The Budget estimate (body bytes / 2) is 1.33–1.43 times the billed tokens, and a test holds it to these recordings.
