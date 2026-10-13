# CONTEXT — domain glossary

The names kiroku's code, docs, and design discussions use. If a discussion needs a concept that isn't here, add it.

## Streams and audio

- **Channel** — a streamer kiroku watches, e.g. `caedrel` or `gorgc` on Twitch. Channels live on a watch list.
- **Source** — where a stream's audio comes from. Twitch first, using streamlink piped into ffmpeg. YouTube is a later Source implementation.
- **Stream session** — one continuous live broadcast of a Channel, from going live to going offline.
- **Category** — the game or category Twitch reports for the stream (`League of Legends`, `Dota 2`, `Just Chatting`). It can change mid-session.
- **Segment** — the part of a Stream session spent in one Category. A Category change starts a new Segment. `segment` stores Category, title and Pack at an audio-clock boundary; title updates also record a boundary. A Window uses the Segment at its start, including during recovery.

## Transcription and speakers

- **Capture leg** — one streamlink→ffmpeg run within a Stream session. Each leg has its own archive file. Legs sit back to back on the session's audio clock, and each anchors that clock to wall-clock time. A stream drop or reconnect starts a new leg in the same session. A leg that a crash left open is **sealed** at its archive's decoded length before the next leg is placed.
- **Transcript span** — a range of the session's audio clock that a Transcriber has finalized, silent ranges included. Its row commits atomically with its Utterances and Cost ledger row. The highest span end is the "transcript complete up to here" watermark.
- **Gap** — a part of a Capture leg that no Transcript span covers. Gaps appear when a crash loses audio that was queued for transcription.
- **Backfill** — transcribing Gaps from the archive. It uses the same Transcriber and commit path as live audio, as background audio: its calls wait for every live chunk on the same API key and use only spare capacity.

- **Utterance** — one finalized piece of transcript from the Transcriber. It has start and end timestamps and a Speaker label.
- **Speaker label** — the anonymous speaker id the Transcriber assigns (`speaker_A`). It is only stable within one transcription connection.
- **Speaker map** — the mapping from Speaker labels to roles: `streamer`, `caster`, or `other`. It is decided per session and re-decided after a reconnect or when a new label appears.
- **Streamer turn** — an Utterance whose Speaker label maps to `streamer`. The Gate and Notes read Streamer turns; caster turns are kept only as context.

## Domain knowledge

- **Pack** — a game-specific plug-in chosen by Category: `league` and `dota`. A Pack provides the Vocabulary, a Notes schema for each Mode, and optional Live context tools. The core works with no Pack and simply has less context.
- **Mode** — what the streamer is doing within a Pack: `playing`, `watching_pro`, `chatting`, or `other`. The Gate infers it for each Window.
- **Vocabulary** — the list of terms a Pack supplies for the Transcriber's keyword biasing: champions or heroes, items, players, and slang. League loads Data Dragon and current rosters, then selects match terms or title-relevant names to fit Whisper's prompt.
- **Live context tool** — a Pack function that returns the live game state, such as `get_live_match()` (teams, picks, game number). It is optional. League writes its compact summary to `context.live`; bans are `null` when the source does not expose them. A new snapshot does not apply to Windows starting before its Game boundary.
- **Game key** — an identifier for "the game currently being discussed", used to scope deduplication. Examples: `league:<lolesports event id>:<game number>`, or a Dota match id. The League Gate stores it on `gate_window` only for Mode `watching_pro` with live context. Missing context means a `null` Gate key. Alerts then use `seg:<session>:<segment start ms>:<anchor window start ms>`. A Segment change or a gap of more than 20 minutes on the Rule starts a new coarse key.

## Notes

- **Window** — a 45 s slice of a session's audio clock that the Gate evaluates. Windows sit back to back; the last one of a session can be shorter. Stored as `gate_window` (`window` is reserved in SQL), with the exact state the Gate saw: `new` (what was said in the Window) and `context` (stream details and a short tail of earlier speech).
- **Notes** — structured summaries of 180-second spans on the session audio clock. A closed session also gets a final partial span. The Segment at the span start selects the Pack; answered Windows select Mode by majority, with ties going to the latest. Pack schemas and the core `general` schema carry a name and version. Pydantic validates the body, and every `at_ms` must lie inside the source span. Pending spans retain attempts and costs without advancing the timeline. Empty and failed spans still advance it. Inputs and raw attempts form the distillation dataset. `kiroku notes list` reads the timeline; `kiroku notes export --since DATE` writes JSONL.

## Alerts

- **Rule** — a natural-language alert condition written by the user, e.g. "he predicts which team wins the current game". Each Rule has a threshold and a cooldown.
- **Gate** — stage 1 of alerting. One Jev request per Window evaluates every active Rule as a yes/no question and returns a probability for each.
- **Rule test** — a dry run of one Rule, or a candidate rewording, against stored Windows: the same Gate request without Mode, scored but not stored as Gate decisions. It is billed to the month, not to a session.
- **Gate decision** — one (Window, Rule) result: the probability, the threshold, and whether it passed. Every Gate decision is logged. When the Rule wasn't asked (no speech, Budget cap, Gate failure), the probability is empty and the Window's status says why.
- **Extraction** — stage 2. Runs only when a Gate decision passes. The Groq JSON LLM writes the alert content and checks whether the content is new, using the saved Gate question and state.
- **Novelty check** — Extraction's comparison with the last delivered or possibly delivered claim for the same Rule, Channel and Game key. Content that is not new is dropped.
- **Cooldown** — the minimum speech-time gap between delivered or possibly delivered Alerts for a Rule on a Channel. Suppressed Alerts do not restart it.
- **Alert** — the durable outcome of a passing Gate decision: `stale`, `cooldown`, `duplicate`, `sending`, `sent`, `unconfirmed` or `failed`. Deliverable Alerts send one message through a Notifier.
- **Notifier** — an output channel for Alerts. Telegram first; Discord later.
- **Label** — a human judgment on a Gate decision. Labels come from Telegram 👍/👎 buttons or from the Review queue.
- **Review queue** — borderline Gate decisions (probability 0.3–0.7) shown in the web app for labelling, so recall can be measured and not just precision. A Review label is independent of the Telegram label for the same decision. The latest Review verdict wins; undo removes only that Review label.

## Operations

- **Cost ledger** — one row per paid call (transcription, Jev, LLM) with its cost. It is the source of truth for Budget caps.
- **Budget cap** — hard per-session and per-month spending limits. Paid calls stop when a cap is reached.
- **Fallback** — the replacement used when a vendor is unavailable. The Transcriber falls back to Groq Whisper in chunks, with no speaker labels. The Gate falls back to Groq `openai/gpt-oss-120b` answering the same Rule and Mode questions.

- **Live transcript stream** — the web app's SSE feed of committed Utterances. Event IDs are session audio offsets and group speech at the same offset. Backfill behind the cursor triggers reconciliation from stored pages.
