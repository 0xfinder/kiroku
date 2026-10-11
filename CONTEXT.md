# CONTEXT — domain glossary

The names kiroku's code, docs, and design discussions use. If a discussion needs a concept that isn't here, add it.

## Streams and audio

- **Channel** — a streamer kiroku watches, e.g. `caedrel` or `gorgc` on Twitch. Channels live on a watch list.
- **Source** — where a stream's audio comes from. Twitch first, using streamlink piped into ffmpeg. YouTube is a later Source implementation.
- **Stream session** — one continuous live broadcast of a Channel, from going live to going offline.
- **Category** — the game or category Twitch reports for the stream (`League of Legends`, `Dota 2`, `Just Chatting`). It can change mid-session.
- **Segment** — the part of a Stream session spent in one Category. A Category change starts a new Segment.

## Transcription and speakers

- **Capture leg** — one streamlink→ffmpeg run within a Stream session. Each leg has its own archive file. Legs sit back to back on the session's audio clock, and each anchors that clock to wall-clock time. A stream drop or reconnect starts a new leg in the same session.
- **Transcript span** — a range of the session's audio clock that a Transcriber has finalized, silent ranges included. Its row commits atomically with its Utterances and Cost ledger row. The highest span end is the "transcript complete up to here" watermark.

- **Utterance** — one finalized piece of transcript from the Transcriber. It has start and end timestamps and a Speaker label.
- **Speaker label** — the anonymous speaker id the Transcriber assigns (`speaker_A`). It is only stable within one transcription connection.
- **Speaker map** — the mapping from Speaker labels to roles: `streamer`, `caster`, or `other`. It is decided per session and re-decided after a reconnect or when a new label appears.
- **Streamer turn** — an Utterance whose Speaker label maps to `streamer`. The Gate and Notes read Streamer turns; caster turns are kept only as context.

## Domain knowledge

- **Pack** — a game-specific plug-in chosen by Category: `league` and `dota`. A Pack provides the Vocabulary, a Notes schema for each Mode, and optional Live context tools. The core works with no Pack and simply has less context.
- **Mode** — what the streamer is doing within a Pack: `playing`, `watching_pro`, `chatting`, or `other`. The Gate infers it for each Window.
- **Vocabulary** — the list of terms a Pack supplies for the Transcriber's keyword biasing: champions or heroes, items, players, and slang.
- **Live context tool** — a Pack function that returns the live game state, such as `get_live_match()` (teams, picks, game number). It is optional.
- **Game key** — an identifier for "the game currently being discussed", used to scope deduplication. Examples: a lolesports match plus game number, or a Dota match id.

## Notes

- **Window** — a 45 s slice of a session's audio clock that the Gate evaluates. Windows sit back to back; the last one of a session can be shorter. Stored as `gate_window` (`window` is reserved in SQL), with the exact state the Gate saw: `new` (what was said in the Window) and `context` (stream details and a short tail of earlier speech).
- **Notes** — structured, timestamped summaries produced every few minutes by the Notes model. Their shape comes from the Pack's Notes schema for the current Mode. The notes step is the distillation target.

## Alerts

- **Rule** — a natural-language alert condition written by the user, e.g. "he predicts which team wins the current game". Each Rule has a threshold and a cooldown.
- **Gate** — stage 1 of alerting. One Jev request per Window evaluates every active Rule as a yes/no question and returns a probability for each.
- **Gate decision** — one (Window, Rule) result: the probability, the threshold, and whether it passed. Every Gate decision is logged. When the Rule wasn't asked (no speech, Budget cap, Gate failure), the probability is empty and the Window's status says why.
- **Extraction** — stage 2. Runs only when a Gate decision passes. Haiku writes the alert content and checks whether the content is new.
- **Novelty check** — Extraction's comparison with the last Alert sent for the same Rule and Game key. Content that is not new is dropped.
- **Cooldown** — the minimum time between Alerts for a Rule.
- **Alert** — a message sent through a Notifier.
- **Notifier** — an output channel for Alerts. Telegram first; Discord later.
- **Label** — a human judgment on a Gate decision. Labels come from Telegram 👍/👎 buttons or from the Review queue.
- **Review queue** — borderline Gate decisions (probability 0.3–0.7) shown in the web app for labelling, so recall can be measured and not just precision.

## Operations

- **Cost ledger** — one row per paid call (transcription, Jev, LLM) with its cost. It is the source of truth for Budget caps.
- **Budget cap** — hard per-session and per-month spending limits. Paid calls stop when a cap is reached.
- **Fallback** — the replacement used when a vendor is unavailable. The Transcriber falls back to Groq Whisper in chunks, with no speaker labels. The Gate falls back to Haiku answering yes/no with a probability.
