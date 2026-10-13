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

## 2026-10-12: Groq Gate Fallback (issue #5)

`uv run python spikes/gate_fallback.py spikes/results/2026-10-11-gate-split.jsonl spikes/results/2026-10-12-gate-fallback.jsonl --repeats 3` runs the production LLMGate over eight stored split Windows. Calls are paced at 16 seconds outside the provider retry deadline. All 24 successful calls wrote ledger rows in the isolated `kiroku_spike_5` database, totaling $0.007180 at list price. The dev database was not migrated.

Each cell is Jev / gpt-oss min-max over three repeats, with the original thresholds 0.60 for predicts_winner and 0.70 for the other Rules.

| Chunk | predicts_winner | draft_opinion | talks_item |
| --- | --- | --- | --- |
| 0 | 0.68-0.69 / 0.80-0.85 | 0.14-0.19 / 0.10-0.10 | 0.75-0.84 / 0.05-0.05 |
| 1 | 0.10-0.13 / 0.00-0.05 | 0.95-0.96 / 0.85-1.00 | 0.15-0.20 / 0.00-0.02 |
| 2 | 0.06-0.06 / 0.00-0.00 | 0.89-0.89 / 0.85-0.90 | 0.43-0.57 / 0.00-0.00 |
| 3 | 0.11-0.12 / 0.00-0.00 | 0.05-0.05 / 0.00-0.00 | 0.78-0.83 / 0.60-0.80 |
| 4 | 0.24-0.27 / 0.05-0.10 | 0.95-0.96 / 0.85-0.90 | 0.80-0.84 / 0.10-0.20 |
| 5 | 0.11-0.12 / 0.00-0.00 | 0.90-0.90 / 0.90-1.00 | 0.36-0.40 / 0.00-0.00 |
| 6 | 0.08-0.09 / 0.00-0.00 | 0.82-0.86 / 0.00-0.00 | 0.98-0.98 / 0.90-1.00 |
| 7 | 0.66-0.68 / 0.00-0.10 | 0.40-0.44 / 0.00-0.20 | 0.91-0.93 / 0.85-0.85 |

Mode was `watching_pro` in all 24 calls, matching Jev. The chunk-0 winner prediction passed in 3/3 repeats, and chunk 1 stayed below threshold in 3/3. The implicit chunk-7 prediction scored 0.00-0.10 and failed in 3/3, while Jev scored 0.66-0.68. Several item decisions also differ. Fallback probabilities are not interchangeable with Jev probabilities, and the existing thresholds are not calibrated for gpt-oss. This records a degraded outage path rather than equivalent classifier quality.

The canonical request bodies and real responses are in [2026-10-12-gate-fallback.jsonl](results/2026-10-12-gate-fallback.jsonl). Offline tests force Jev connect failure, prove one primary attempt per bypass, compare all passed values to the recording, and verify Groq gate ledger rows.

## 2026-10-12: Extraction, Novelty and Telegram (issue #5)

`uv run python spikes/alert_extraction.py spikes/results/2026-10-11-gate-split.jsonl spikes/results/2026-10-12-alert-extraction.jsonl --repeats 3` runs the production AlertWorker, Extraction parser and Groq adapter over repeat 0 of Jev's split decisions. Each repeat has default Cooldown 600 seconds and Cooldown 0. The saved fixture has **12** passing decisions, not the design's stated 13: two winner predictions, five draft opinions and five item decisions. The measurement follows the actual recorded decisions.

All 45 calls succeeded and wrote ledger rows in the isolated migrated `kiroku_spike_5_extraction_v3` database, totaling **$0.012539** at list price. Requests are paced at 16 seconds. A separate synthetic speech day per arm keeps Cooldown histories independent, and the worker's clock is fixed to that replay day. Telegram delivery is a local sink; this measures Extraction and suppression, not real said-to-sent latency. The dev `kiroku` database was not migrated.

| Arm | Repeat | LLM calls | Sent | Duplicate | Cooldown | Winner chunk 7 |
| --- | --- | --- | --- | --- | --- | --- |
| default | 0 | 3 | 3 | 0 | 9 | cooldown |
| novelty_only | 0 | 12 | 10 | 2 | 0 | duplicate |
| default | 1 | 3 | 3 | 0 | 9 | cooldown |
| novelty_only | 1 | 12 | 10 | 2 | 0 | duplicate |
| default | 2 | 3 | 3 | 0 | 9 | cooldown |
| novelty_only | 2 | 12 | 9 | 3 | 0 | duplicate |

The final prompt judges the chunk-7 prediction to be the same as chunk 0 in **3/3 Novelty-only repeats**, so each arm sends exactly one winner Alert. The first prototype reclassified the implicit prediction as absent; its first complete Novelty arm sent chunk 7. Revision 2 improved that to **2/3**. The final prompt distinguishes the classifier's quoted instructions from Extraction's instructions and gives a generic Red-vs-Blue example of the same predicted winner expressed as its opponent's inability to fight. It contains no Spirit/PARIVISION-specific shortcut. Three successful repeats on one fixture do not establish general reliability. Other suppression decisions vary between repeats.

The interrupted first prototype is in [alert-extraction-prototype.jsonl](results/2026-10-12-alert-extraction-prototype.jsonl), revision 2's complete 45-call run is in [alert-extraction-revision-2.jsonl](results/2026-10-12-alert-extraction-revision-2.jsonl), and the final requests/responses and every alert outcome are in [alert-extraction.jsonl](results/2026-10-12-alert-extraction.jsonl). The initial simultaneous smoke/Extraction setup contaminated Cooldown history and was stopped; the measured runs use separate databases. Exploratory ledger totals are retained in `2026-10-12-alert-ledger-summary.json`; they are not included in the final 45-call cost.

`uv run python spikes/telegram_smoke.py tests/fixtures/telegram` sent **one real** production-rendered message with 👍/👎 buttons. The test marker was originally in its body and was subsequently moved to the first line with `editMessageText`, preserving the same message and buttons. The sanitized real send, prefix correction and 20 minutes of empty callback polls are in [the Telegram fixture](../tests/fixtures/telegram/smoke.json). **No human tap arrived within 20 minutes.**

`uv run python spikes/telegram_smoke.py tests/fixtures/telegram --complete-timeout` then replayed Bot-API-documented callback, acknowledgement and markup shapes through the production parser, `labels.record`, and `TelegramNotifier.confirm`. It verified `(decision_id=1, applies=true, source=telegram, alert_id=1)` in the scratch database. [synthesized.json](../tests/fixtures/telegram/synthesized.json) and its [README](../tests/fixtures/telegram/README.md) explicitly mark those shapes and that Label as synthesized. Neither a real human Label nor live `answerCallbackQuery`/`editMessageReplyMarkup` success is claimed.

Offline acceptance replays all three Extraction repeats by canonical request body, uses the recorded Bot API Message shape, verifies duplicate linkage and Cost ledger rows, and labels the stored Gate decision through the documented callback shape. Failure tests cover uncertain sends, restart recovery, Label redelivery and changed verdicts, staleness, per-channel Cooldown, Pack keys, Budget refusal, provider failures, quota expiry and concurrent admission, poller ownership handoff, lost database acknowledgements and worker bugs. Synthetic audio through real ffmpeg proves a blocked Telegram send does not stall transcription or the Gate.

Still unmeasured: said-to-sent latency and human Labels from a live pro match after merge. The ten-minute freshness cutoff and twenty-minute coarse-key gap remain initial values to tune from that measurement.

## 2026-10-13: Rolling Notes with per-Pack schemas (issue #7)

`uv run python spikes/notes_replay.py spikes/results/2026-10-13-notes.jsonl --database kiroku_spike_7_notes_final_v3` runs the production NotesWorker and Groq adapter over two stored transcripts, three repeats each. The scratch database is isolated and migrated by the script. The dev database is untouched. Calls are paced at 16 seconds, use `openai/gpt-oss-120b` with low reasoning effort and temperature 0, and commit `Purpose.NOTES` costs under each fixture session.

The League fixture is #6's 611.072-second Caedrel RED Kalunga–FlyQuest transcript (`2026-10-12-league-vocabulary/pack.jsonl`). Production transcript and Gate commit functions seed its Utterances and answered `watching_pro` Windows. The recorded completed-match snapshot from #6 supplies team and champion spelling. Three 180-second spans and a 71.072-second final span produce four `league.watching_pro` Notes per repeat. The no-Pack fixture is #1's 240-second Gorgc transcript (`2026-10-11-gorgc-blast-slam.jsonl`), with a 180-second span and a 60-second final span using `general`. Each fixture session is closed before Notes run. No live stream or fresh speech recognition is measured here.

The [final recording](results/2026-10-13-notes.jsonl) contains every canonical HTTP request, real response, stored Note, and ledger total. `uv run python spikes/notes_counts.py spikes/results/2026-10-13-notes.jsonl` reproduces the [counts](results/2026-10-13-notes-metrics.json).

- **Validity:** 18 of 18 calls and terminal Notes validate, without retries. All 18 summaries are nonempty.
- **Timestamps:** all 90 timestamped items fall inside their source span. The parser rejects an item at the exclusive end boundary, a negative timestamp, or a boolean masquerading as an integer.
- **Teams and draft:** all 12 League Notes name RED Kalunga and FlyQuest. Teams, game number, and draft carry the recorded match state; that state is not evidence of newly spoken events.
- **Prediction capture:** all three repeats preserve “RED should be still favorable” at 518.940 seconds as a take. All 12 `predictions` arrays are empty, even after the writing instructions explicitly included a named favorite. This fixture does not prove reliable structured prediction capture. The Gorgc 70–30 Spirit call appears in the general schema's takes in all three repeats.
- **Cost:** the 18 successful calls total **$0.008709**, matching both response token usage at list price and the Notes ledger. League repeats cost $0.001859, $0.001679, and $0.001977. General repeats cost $0.001096, $0.001031, and $0.001067.

Two earlier recordings remain for review. The [prototype](results/2026-10-13-notes-prototype.jsonl) copied previous timestamped items into later spans: 12 Notes succeeded, five became `invalid`, and one span remained pending after a provider deadline. It made 24 HTTP calls. Response usage cost $0.014797, but its stored ledger reported $0.034152 because an early adapter edit double-counted the estimate after parse failures. The final adapter and a regression test correct that accounting error. The [second revision](results/2026-10-13-notes-revision-2.jsonl) stopped copying old timestamps and produced 18 valid Notes for $0.007306, but six League summaries were empty. The final schema requires a nonempty summary and the prompt explicitly requests one. These prototypes are evidence of the iteration, not the CI or distillation target.

Offline CI matches canonical request bodies against all three final repeats, then compares validated bodies and ledger totals. Failure tests prove one retry and both raw outputs for invalid responses, progress on later spans, empty spans without calls, unavailable retry and expiry, budget refusal, process-wide auth disablement, exactly-once commits under racing receipts, restart recovery, Mode ties, historical Pack selection, the 12-hour limit, and unknown Mode without the Gate. Attempt records retain the original input, system prompt, and model across a schema change on restart. Export streams rows through a server cursor. Synthetic audio through real ffmpeg proves a hung Notes call cannot stop transcription or gating. Cancellation tests cover paid retries, budget checks, HTTP dispatch, returned-result commits, and an unavailable database.

Limits remain. Validation proves shape and timestamp bounds, not factual accuracy or completeness. For example, one summary says RED is “crushing fights” where the transcript says “butchering all the fights.” Background casters remain mixed into these transcripts until Speaker maps exist. The quota reserve and shutdown timings are operating choices, not throughput measurements. A hard kill or sustained database outage can lose an attempt before its journal commits; its span remains pending for restart.
