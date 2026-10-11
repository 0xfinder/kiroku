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
