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

## 2026-10-12: Rule wording with `rule test` (issue #4)

**Setup:** `KIROKU_DATABASE_URL=<migrated db> uv run python spikes/rule_wording.py spikes/results/2026-10-12-rule-wording.jsonl`. The script runs the production Rule test path (`kiroku.dry_run.run`, which is what `kiroku rule test` calls) over the spike's eight Windows. It uses the exact state the Gate saw, and compares two wordings of `draft_opinion` at threshold 0.70:

- **old** (the spike's): "Does the speaker give an opinion on a team's draft or hero picks?"
- **new**: "Does the speaker talk about picking or banning heroes in this game's draft, such as why a hero was picked, banned, or should have been picked?"

| Chunk | old | new | What was said |
| --- | --- | --- | --- |
| 0 | 0.16 | 0.03 | play-by-play, a win-probability call |
| 1 | **0.96** | **0.97** | "this ban and block pick, they know Yatora plays Anti-Mage" |
| 2 | **0.89** | **0.86** | "if I'm ST here I would like an Axe" |
| 3 | 0.05 | 0.03 | play-by-play |
| 4 | **0.95** | **0.95** | "Ursa is one of the highest win rates against Anti-Mage... picked it for a reason" |
| 5 | **0.90** | 0.23 | "Lycan does not scale Giga well, but he's super farmed" |
| 6 | **0.82** | 0.11 | "he is actually very good at hunting the supports" (an item build) |
| 7 | 0.44 | 0.07 | teamfight and items |

**Findings:**

1. **The old wording passes any talk about heroes:** 5 of 8, including in-game remarks on scaling and builds (chunks 5 and 6).
2. **The reworded Rule passes exactly the three Windows about picks and bans**, and nothing else scores above 0.23. Naming the concrete events ("picked, banned, should have been picked") separates better than an abstract category ("opinion on the draft"). Two other attempts did worse: one explicitly excluded in-game talk, the other asked about "the draft phase".
3. Each wording took 8 calls, about $0.0003, in about 2 s.

## 2026-10-12: League Vocabulary and live context (issue #6)

**Setup:** downloaded Caedrel's [2026-10-08 co-stream](https://www.twitch.tv/videos/2895001169), RED Kalunga vs FlyQuest game 1, with `.venv/bin/python -m streamlink --hls-start-offset 01:10:00 --hls-duration 00:10:00 --output /tmp/kiroku-6-data/caedrel-2895001169.ts twitch.tv/videos/2895001169 audio_only`. HLS segment boundaries yielded 611.072 s, not exactly 600. ffmpeg decoded mono 16 kHz PCM outside the repo. The clip includes a match pause and a chat story about China. Its first 160 s and final 210 s contain pro-game commentary, mixed with background casters.

`KIROKU_DATABASE_URL=<migrated spike db> uv run python spikes/league_vocabulary.py /tmp/kiroku-6-data/caedrel.pcm spikes/results/2026-10-12-league-vocabulary` runs the production SilenceChunker and Groq adapter, pinned to `whisper-large-v3-turbo`, temperature 0. Both arms use identical audio boundaries and each arm carries its own previous text. Each has 32 requests and 611.072 audio seconds. Every successful call wrote a Cost ledger row in the isolated `kiroku_spike_6` database. The two final arms cost $0.013582 together; a third prototype arm brought the total to 96 calls, 1,833.216 billed audio seconds and $0.020373. Groq returned 429s, which the production adapter retried. All 96 requests completed and all spans are present.

The control uses Category plus the VOD title. The final Pack prompt uses Data Dragon's canonical names and the ten actual match players/champions from the completed-match window feed. Current `getTeams` rosters are loaded too. A prototype included registered substitutes and the title; it consumed 121–189 Whisper tokens. It is retained as `pack-with-substitutes.jsonl`. The shipped choice omits substitutes/title when live match terms exist, consumes 82–152 tokens, and leaves room for the 200-character previous-text tail. The title control consumes 26–159 tokens. Counts use OpenAI Whisper's `multilingual.tiktoken` ranks and pattern with `tiktoken`, not a words-per-token estimate. Character bounds remain a heuristic for other text.

**Name audit:** [audit.json](results/2026-10-12-league-vocabulary/audit.json) lists every reference occurrence and error. Careful reading of aligned grammatical English clauses, checked against the game's ten champion/player names, produced 36 reference occurrences. Case does not matter; canonical spelling does. The reference comes from reading the two outputs, not listening. It cannot find names missed by both arms. Ambiguous caster garble and the control's repetitive ungrammatical `DRAKE, Rumble` strings are explicitly excluded; both complete outputs remain committed so that this choice is reviewable. This is a name audit, not audio-grounded WER.

| Error count in the 36-occurrence audit | Title only | Final Pack |
| --- | --- | --- |
| Champion names | 12 | 9 |
| Match player names | 3 | 0 |
| Misspellings | 15 | 8 |
| Visible omissions | 0 | 1 |
| Total | 15 | 9 |

`uv run python spikes/league_counts.py spikes/results/2026-10-12-league-vocabulary` checks the annotations against the stored outputs and recounts them. `Griffin` became `Gryffinn`, both `Gatgos` mentions became `Gakgos`, and `Eunara`/`Yuen Arna` and `Lulul` became `Yunara` and `Lulu`. `Xindra`/`Zindra` and `Mukong` remain. The Pack regressed a correct `Ornn` to `Oren` and omitted the isolated `Rumble` before the final damage comparison. A single comparison supports a six-error reduction on these audited phrases, not a general accuracy guarantee. The prototype is not the comparison reported in the table.

**Real API recording:** [tests/fixtures/league](../tests/fixtures/league/README.md) contains `getLive` (0 events), `getSchedule` (80 events), the completed RED–FLY event, its game's window/details feeds, both teams' current rosters, and name-only Data Dragon catalogue projections. The source client from `0xfinder/live-lol-esports` supplied the endpoints, public website key and delayed ten-second feed clock. Picks and player names come from window metadata; the details endpoint contains participant statistics, not bans. None of these responses exposes bans, so the live summary uses `bans: null`.

**Gate measurement:** `KIROKU_DATABASE_URL=<migrated spike db> uv run python spikes/league_gate.py spikes/results/2026-10-12-league-gate.jsonl` made 12 real Jev calls, three repeats of two recorded passages, with and without the compact live summary. Every call uses the production provider and writes its ledger row. On the passage saying Yunara is broken, the concrete `yunara_broken` Rule scored 0.98 in all three live repeats, versus 0.98–0.99 without live data. On chat about China it scored 0.01 in both layouts. The draft Rule scored 0.10–0.11 without live data and 0.60–0.62 with it on in-game commentary, below threshold 0.70. Chatting with a live match present was Mode `watching_pro` on one of three repeats; the other two were `chatting`. These are measured limitations of the context, not a claim that Mode is perfect. All requests/responses, including that mistake, are committed and replayed byte-for-byte in CI. No wording or model changed, and the existing no-Pack Gate cassette still passes unchanged.

**Observable integration:** synthetic recorded audio through real ffmpeg, the watcher, Groq/HTTP replays and Postgres proves that Category `League of Legends` selects the Pack, Gate state contains both teams and all ten picks, League-scoped Rules run, and `watching_pro` stores `league:117133830114188936:1`. HTTP failures keep transcription and the Gate running with title-only and no live context. Category changes clear live state; stored Segment history preserves scoped Rules during closed-session recovery. A restart backlog receives no current live summary/key. There was no live pro match to verify the feed clock. That acceptance step remains unverified until a match is on.
