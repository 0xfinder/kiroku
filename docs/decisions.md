# Decisions

One entry per significant design decision: what was decided, why, what was rejected, and when to revisit it. Add new entries at the end. Don't edit a superseded entry; mark it `Superseded by Dn` and write a new one.

Terms are defined in [CONTEXT.md](../CONTEXT.md).

## D1. A portfolio piece and a personal tool; the portfolio wins conflicts

kiroku is a personal tool. It is also the project behind an application for AI-engineering roles, so its quality claims are measured, not asserted: transcription error rate on game terms, Gate precision and recall, and the cost and quality of distillation. It is also a deliberate exercise in agentic AI, so each AI component has to justify its existence.

## D2. Single user

There is no account system. Rules carry an `owner` field so that adding users later doesn't need a migration of the alert model.

## D3. Muse Voice Transcribe for live transcription

Muse is Meta's streaming speech-to-text model: about 0.16 s to a final transcript, speaker labels in the same pass, keyword biasing, and $0.18 per audio hour (about $1.10 per 6-hour stream).

- **Rejected: local faster-whisper.** The host has 2 vCPUs, 3 GB of RAM, and no GPU.
- **Rejected: Groq Whisper as the primary.** It's cheaper, but has no speaker labels and adds chunking latency. It stays as the Fallback and as an offline comparison baseline.
- **Rejected: Deepgram.** It costs more for a similar feature set.
- **Revisit if:** our own measurements show Muse's speaker-labelling error is well above the vendor's 17.5%, or its accuracy on game terms is worse than Whisper's.

## D4. Two-stage alerting: a Jev Gate, then Haiku Extraction

Stage 1, the Gate: one Jev request per Window answers every Rule as a yes/no question with a calibrated probability. It costs about $0.04 per million input tokens and takes 0.1–0.5 s. Stage 2: for Rules that pass, Haiku writes the Alert content and runs the Novelty check.

Jev's limits shape the design:

- It returns no explanation, so stage 2 is needed to write the Alert.
- Accuracy drops with irrelevant input, so a Window holds only Streamer turns plus a compact live-context summary.
- It is closed and can't be fine-tuned.

- **Rejected: keyword-only Rules.** Too noisy, and they can't express events like "he predicts a winner".
- **Rejected: embedding similarity as the first stage.** It fits fuzzy topics, not events. It may come later for archive-wide topic alerts.

## D5. The alert pipeline is a fixed sequence of steps, not an agent

Its steps never vary: transcribe → Window → Gate → Extraction → notify. An agent would add cost and nondeterminism and buy nothing. Agents belong where the steps depend on the question (D6).

## D6. Q&A agent: a hand-written tool loop first, then LangGraph

The first version is a roughly 100-line tool-calling loop on the Anthropic SDK, written to understand the mechanics. It is then ported to LangGraph, with a written comparison of what the framework actually provided (tracing, state, interrupts, evals) against what it cost.

Tools: `get_live_match`, `get_segment(start, end)`, `search_notes(filters)`, and later `search_archive` and `get_stats` (senrigan).

## D7. Model assignment

Every model sits behind a thin provider interface, so any of them can be swapped.

| Job | Model |
| --- | --- |
| Live transcription | Muse Voice Transcribe (Fallback: Groq `whisper-large-v3-turbo`) |
| Gate, Speaker map, Mode | Jev (Fallback: Haiku) |
| Notes, Extraction, Novelty check | Claude Haiku 5.5 (`claude-haiku-5-5`) |
| Q&A agent | Claude Sonnet 5.5 (`claude-sonnet-5-5`) |

## D8. Budget of at most $20 a month, enforced in code

Every paid call writes a Cost ledger row. Budget caps per session and per month stop paid calls when reached, so a looping bug can't burn money.

## D9. Postgres with pgvector

Postgres runs on the host. Notes are stored as JSONB, validated per Pack and Mode by Pydantic schemas that carry a `schema_version`. Embeddings are added once an archive exists: a hybrid of pgvector, Postgres full-text search, and filters on game and date. Keyword search matters because player and champion names match better by keyword than by embedding.

## D10. Front ends: a Telegram bot plus a responsive web app; no TUI

The user reads on both phone and PC.

- **Telegram:** pushes Alerts with 👍/👎 buttons and answers `/ask`.
- **Web (SvelteKit + FastAPI, live updates over SSE):** live transcript, Notes timeline, Rule editor, Review queue, and an agent chat that shows each tool call.

Discord is added later as a second Notifier.

## D11. Category selects the Pack; Jev infers the Mode

Twitch's category is free and reliable, so it selects the Pack and is re-checked on every poll. The Mode (`playing`, `watching_pro`, `chatting`, `other`) has to be inferred, which suits a Jev choice question.

## D12. League first, Dota second; the Pack interface is extracted, not designed

The core and the League Pack are built together. The Dota Pack is the second implementation. Whatever had to change in the core to support Dota defines the real Pack interface, and those changes are recorded here.

A Pack provides a Vocabulary, a Notes schema per Mode, and optional Live context tools. Because the tools are optional, Dota can ship before live Dota data is proven.

## D13. Speaker map from Jev

A Jev choice question over sample turns from each Speaker label decides which label is the streamer. The decision is cached per session and re-decided after a reconnect or when a new label appears. Below about 0.8 confidence it falls back to Haiku. Every mapping is logged so its error rate can be measured. An offline pyannote pass is a possible measured improvement later.

## D14. Alert deduplication: a Cooldown plus a Novelty check per Game key

One discussion of a draft can pass the Gate in many consecutive Windows. An Alert is sent only when its content is new for that Rule and Game key, for example a new prediction or a changed pick.

## D15. Labelling built into v1

Telegram 👍/👎 taps measure precision. The web Review queue of borderline decisions (probability 0.3–0.7) measures recall. Thresholds per Rule are tuned from these Labels. This produces the labelled dataset at no extra cost.

## D16. Fine-tuning target: distilling the Notes step, after v1

Muse and Jev are both closed, so the Whisper LoRA and Gate-distillation plans are dropped. The Notes step runs all stream long and has a fixed task, which makes it the right place to fine-tune: collect Haiku's outputs, LoRA-tune a small open model, and compare quality, cost, and latency.

## D17. Audio retention

Full audio is kept for 14 days as Opus at 32 kbps (about 85 MB per 6-hour stream). Labelled evaluation clips are kept forever. Transcripts and Notes are kept indefinitely in Postgres.

## D18. Hosting

Everything runs on the current host: Postgres via Docker Compose and the app under systemd. The web app is reachable through Tailscale or Cloudflare Tunnel with Access, never exposed to the public internet. Single-user (D2) makes this simpler than adding authentication.

## D19. YouTube only through the Source interface

Audio acquisition sits behind `Source`. No YouTube code gets written until Twitch works from start to finish.

## D20. Vendor risk is handled by Fallbacks

Muse and Jev were both about a month old when chosen, and Jev is in early access. Both sit behind interfaces with working Fallbacks, which also serve as comparison baselines.

## D21. Live detection through streamlink metadata, not the Twitch Helix API

`streamlink --json twitch.tv/<channel>` returns live status, category, and title with no Twitch credentials. Verified on 2026-10-11: Gorgc returned `category: "Dota 2"` with a co-stream title, and Caedrel (offline) returned "No playable streams". streamlink depends on the same unofficial Twitch endpoints for the audio itself, so using it for live detection adds no new point of failure. It also means one fewer credential.

- **Rejected: Helix polling.** It needs a registered Twitch app and an app access token, and buys nothing extra while streamlink is the audio path anyway.
- **Rejected: EventSub webhooks.** They need a public HTTPS endpoint, which conflicts with D18.
- **Revisit if:** Twitch rate-limits the metadata polling, or more than about 20 channels need watching.

## D22. Database layer: async psycopg 3, raw SQL, a small in-house migration runner

Chosen for issue #1 by an architect arena. Three independent design candidates (Opus, Sonnet, Haiku) converged on this shape.

- **Async throughout.** The workload is I/O-bound fan-out in one process: capture subprocesses, Groq/Jev/Anthropic calls, the Telegram long-poll, and FastAPI SSE.
- **Raw SQL with one owning module per table.** The schema leans on Postgres features (JSONB, partial unique indexes, generated tsvector, pgvector) that an ORM would only translate away.
- **Forward-only `.sql` migrations named by UTC timestamp.** Each file runs in a transaction with checksum drift detection and an advisory lock. Timestamps rather than counters, so PRs written in parallel never collide on a version number.
- **Secrets are loaded lazily per component**, so `kiroku db migrate` needs no API keys.

- **Rejected: SQLAlchemy plus Alembic.** It adds a second schema model and needs type shims for pgvector and tsvector. Alembic's revision chain also forks heads when parallel branches add migrations.
- **Rejected: sync code with threads.** SSE and the Telegram long-poll would each need a thread-to-async bridge.
- **Rejected: pydantic-settings with one global settings object.** Every command would need every key, or every key would be optional and checked separately at each use.
- **Revisit if:** migrations need rollbacks, or the hand-written runner grows past about 100 lines.

## D23. Live transcript pipeline shape (issue #2); archive files are per capture leg

Chosen by an architect arena: three design candidates (Opus, Sonnet, Haiku) plus a Sonnet cross-judge. Candidate 1 was the base, with grafts from the other two.

- **One archive file per Capture leg**, at `audio/<channel>/<session>/<seq>.opus`. This supersedes D17's single `<session>.opus` path; retention is unchanged. One ffmpeg per leg writes both the Opus file and the PCM the Transcriber reads, so the archive and the transcript share one clock.
  - **Rejected: one chained Ogg file per session.** It needs page scanning, torn-tail repair, and silence padding after a crash, and browser playback of chained Ogg is unverified.
- **Session audio clock.** Legs sit back to back on it; wall-clock time lives in `capture_leg.started_at`. `utterance.spoken_at` is derived in SQL from the leg holding the audio, and an utterance with no leg fails NOT NULL.
- **Session identity is the platform's broadcast id**, which was stable for hours on a live Gorgc stream. A blip, a restart, or a reopen within one broadcast stays one session.
- **The Transcriber is a stream**: `transcribe(audio events, context) -> AsyncIterator[Result]`. Chunking, retries, and rate limits live inside the Groq adapter, because Muse streams and has no chunks. Transcribers never touch the database.
  - **Rejected: `transcribe(chunk)`.** It would force Muse into chunk latency and expose chunking to every caller.
- **Exactly-once.** A `transcript_span` row, its utterances, and its ledger row commit in one transaction keyed by the span's start. A failed commit is retried; the paid call never is. Silent spans are recorded without a paid call.
- **Silence chunking** uses adaptive 20 ms frame energy in pure Python (about 0.2% of a core): cut at the middle of a 300 ms pause after 15 s, or forced at 25 s on the quietest 100 ms.
  - **Rejected: ffmpeg `silencedetect`.** Its timestamps arrive on stderr, separate from the audio.
  - **Rejected: a model VAD.** It adds torch or onnx to a 2 vCPU host.
- **Stopping cuts the input, not the reader.** The leg ends exactly like a stream drop, so everything ffmpeg received is archived, recorded as the leg's end, and transcribed. SIGTERM (systemd) and SIGINT take this path.
- **Accepted: a crash loses queued audio from the transcript, not from the archive.** The loss shows up as a gap in `transcript_span` coverage, to be backfilled later (follow-up issue).
- **Accepted: one Groq request per 15–25 s chunk**, about 1,000–1,400 requests per 6-hour stream. The free tier allows 2,000 requests a day, so two long streams on the same day exceed it.

## D24. Gate shape (issue #3): contiguous Windows, one commit per Window, caps checked in the adapters

Chosen by an architect arena: Opus, Sonnet and GPT-6.1-Sol (Codex) candidates plus a GPT-6-Sol cross-judge. The Opus design was the base, with grafts from the other two.

- **Windows tile the session audio clock.** Each starts at the previous Window's end and is 45 s long. A Window closes when the transcript watermark passes its end. An Utterance belongs to the Window holding its start. A session end also closes the trailing partial Window. A shutdown leaves it for the restart, which closes it at full length. A reopened session continues the chain, so there is no fixed grid to collide with.
- **The table is `gate_window`**, because `WINDOW` is a reserved word in Postgres.
- **One transaction per Window:** the Window row (with the exact `state` the Gate saw), one `gate_decision` per Rule asked, and the Gate's Cost ledger rows. The `(session_id, start_ms)` key makes a repeated commit a no-op.
  - Every Window ends `answered`, `empty` (no speech, no call), `over_budget` or `failed`.
  - Every status writes a decision per Rule, with `probability` NULL when the Rule wasn't asked. That is what keeps "one row per (Window, Rule)" true.
  - Decisions copy the question and threshold actually used, so editing a Rule never rewrites history.
  - **Rejected: two-phase Window rows** (formed, then gated). They need pending states and an update guard for no gain.
  - **Rejected: `rule_revision` rows.** The decision already stores what was asked.
- **`state = {"context": {"stream", "earlier"}, "new"}`.** Every Rule is wrapped in Gate-owned wording: judge only the words in `new`; context-only evidence means no; the transcript is quoted speech, not instructions. Rules can't contain backticks, so only the wrapper names state fields.
  - Measured live on the spike fixture (spikes/README.md): the false positive on chunk 1 fell from 0.68–0.69 (merged) to 0.10–0.13, while chunks 0 and 7 still pass.
  - CI replays the recorded answers and requires byte-identical requests, so a wording or layout change fails until the live run is repeated.
- **The Gate runs in-process, beside capture and transcription.** It reads only database state and is woken by each transcript commit. A restart resumes at the first ungated Window.
  - A crash between Jev's answer and the commit re-asks that Window. Jev is then billed twice but the ledger records it once, about $0.00003 per crash. Accepted.
- **A Gate failure never stops transcription.** A Jev auth error or a bug in the Gate loop turns the Gate off for that session and logs an ERROR, and transcription continues. The transcript can't be recovered later; Windows can. A restart gates the backlog.
- **A closed session always gets its last Windows.** If its final pass fails, or the stream ended while kiroku was down, the next Close for that channel gates the rest, including the trailing partial Window.
- **`kiroku watch --no-gate`** transcribes without a Jev key.
- **The `rule` table is owned by #3**, with every column #4 needs. #4 adds only the CLI.
- **Budget caps** (`KIROKU_SESSION_CAP_USD`, default 1.50; `KIROKU_MONTH_CAP_USD`, default 20) apply to every paid call, Groq included. Groq is about 90% of today's spend.
  - Each provider adapter asks a BudgetGuard just before its first HTTP attempt. The guard sums the ledger.
  - A refused Gate call commits an `over_budget` Window. A refused Groq chunk is left as a transcript gap, so #10 can backfill it once Budget allows.
  - **Rejected: durable reservations** (a `paid_attempt` table with upper-bound holds), which the cross-judge preferred. They make the cap exact across processes and crashes, but need a reservation lifecycle, crash recovery, and a revision of D23's span-and-cost atomicity.
  - The check-then-call design overshoots a cap by at most the calls already in flight and their retries: a fraction of a cent at today's prices.
  - **Revisit if:** an adapter makes expensive calls concurrently (Sonnet Q&A), or a cap is ever exceeded by more than one call.

## D25. Backfill crash gaps on session start (issue #10)

D23 accepted that a crash loses queued audio from the transcript but not from the archive. Backfill recovers that audio. (Numbered D25 because another branch may add D24 at the same time.)

- **Seal before placing a leg.** A leg with no recorded end gets one before the next leg is placed: its archive's decoded length, read with ffprobe as `duration_ts - initial_padding`. Reading it costs one seek, not a full decode. A full decode runs at about 130x real time, which is minutes for a 6-hour leg.
  - The sealed end is never below the audio already committed, and never past a later leg's start.
  - `sessions.next_leg` raises `UnsealedLeg` while any leg is open, so a leg can't be placed over an archived tail. A recorded end never moves.
  - Capture does the sealing, as part of placing a leg. ffprobe has a 10 s timeout. If ffprobe can't finish (timeout, signal, can't start), the leg stays open and capture retries with its reconnect backoff. Capture's own ffmpeg fails under the same conditions, so this doesn't block live audio in any case where live could have run.
  - An archive that ffprobe rejects, or that is missing, has nothing decodable, so its leg is sealed at the committed audio.
  - `kiroku watch` checks for ffmpeg and ffprobe at launch.
  - **Rejected: estimating the end from wall-clock time.** It would leave gaps or overlaps on the session's audio clock.
- **Gap finding is pure.** `find_gaps(legs, spans)` returns each leg's extent minus the spans of the current Transcriber.
- **Exactly-once.** Gaps come from committed spans, and a commit of a stored span is a no-op. Two backfills of the same gap would still chunk it differently and pay twice, so each backfill holds a per-session, transaction-level advisory lock. Exclusion is needed only in two places:
  - backfill against backfill: the per-session lock above.
  - sealing against live capture: `kiroku backfill` seals a leg only if no `kiroku watch` holds the channel. The check runs in the same UPDATE as the seal.

  `kiroku backfill` never takes the watch lock, so a watch can always start.
- **The same Transcriber path, one stream per gap.** Each gap is decoded from its leg's archive with a pre-rolled `-ss`, which is bit-exact against a full decode. The audio goes through `Transcriber.transcribe` with `Context.background`, prompted with the stored text just before the gap. Silent chunks still cost nothing. Costs reach the ledger through `transcript.commit`, which retries a lost connection for every caller.
- **Behind live audio, never instead of it: a background lane at the shared limiter.** Groq's limits are per key, and one GroqTranscriber serves every session, so priority lives there. It works like this:
  - A background call waits until no live chunk on the key is queued or in flight, and until the key isn't paused after a 429.
  - Background calls start at most once every 10 s per key, across all sessions. That is 6 requests a minute, or about 2x real time. The free tier allows 20 a minute, and live uses 3-4 per channel.
  - Background audio gets backpressure instead of a backlog. It is decoded about one chunk ahead and never skipped.
  - A backfill failure is logged and doesn't stop the session. On stop, backfill finishes the audio it already fed, and the rest resumes on the next start. If the stop itself is cancelled, backfill is cancelled with it.
  - **Rejected: pacing per session.** One version of this PR fed backfill audio at 2x real time per session. Two channels plus backfill could then exceed the key's limits, and the resulting 429 stalled live transcription on every channel.
  - **Rejected: backfill before live.** Live latency would grow by the length of the backlog.
- **`kiroku backfill <channel>`** covers sessions that `kiroku watch` won't resume, for example when the stream ended while kiroku was down. It is safe to run while `kiroku watch` is running.
- **Revisit if:**
  - The Transcriber changes mid-session. Gaps are counted per Transcriber, so the new one would backfill every leg.
  - More channels share a key than the background lane's headroom allows.

## D26. League Pack context and Vocabulary (issue #6)

2026-10-12. The CLI maps Category `League of Legends` to `league` and injects the selector. Core depends on a Protocol with a name, Vocabulary, and an optional live-context call. The first interface contains only what the live transcript uses; Notes schemas wait for Notes. The CLI is the composition root exception to the Pack import ban.

- Free Data Dragon champion/item catalogues and current lolesports rosters use a 24-hour disk cache under the data dir. Parsing precedes cache writes, and invalid cache shapes trigger refetch. lolesports uses its public browser key, overridable with `KIROKU_LOLESPORTS_API_KEY`; it is not a secret or a billed provider call.
- A session refreshes Pack data beside capture every 60 s. A failed refresh clears both outputs, falls back to title-only Vocabulary and no live context, and retries next time. Category/title changes wake the refresh; results from an earlier metadata revision cannot overwrite the current one.
- Whisper cannot hold the catalogue in its 224-token prompt. The live game's actual teams, players and champions take priority; substitutes and unrelated catalogue names stay out. Without a live match, use the title's teams' rosters and catalogue names explicitly mentioned in the title. Vocabulary is at most 350 characters and long Vocabulary leaves a 200-character continuity tail. Final prompts measured 82–152 Whisper tokens on the Caedrel clip. Character limits are a heuristic, not a token guarantee for arbitrary text.
- A live event must match a team name/code in the title and be unambiguous. A game between games has no live summary. The public window feed provides picks and players; the recorded endpoints expose no bans. `bans: null` states that limitation. No champion statistics or item stats enter the Gate.
- The Gate gets `context.live` only when a validated live match exists. Its Mode answer remains authoritative. A Window gets a Game key only when Mode is `watching_pro`. The key is `league:<event id>:<game number>`.
- Category, title and Pack changes are stored on the captured audio clock in `segment`. This small table was added after review found that closed-session recovery otherwise omitted Pack-scoped Rules. A Window uses the Segment at its start. End bounds derive from the next row or the session's end. Title updates also record a metadata boundary; a restart with identical metadata adds no row. A restart removes boundaries beyond the durable resumed clock, because uncommitted capture can have reached further before a crash. A failed metadata write retains its first observed boundary; the watcher retries it before reading another probe, and the Gate waits before processing Windows starting past that pending boundary.
- New live context is bounded by its observed position on the captured audio clock. A restart backlog or a Window starting before a changed game has no live summary/key, rather than receiving the current game's key. Historical live snapshots are not reconstructed; recorded Windows retain the context they actually saw. Pre-feature sessions with no Segment retain their previous unscoped recovery behavior.

Measured results and the reading-only audit are in [spikes/README.md](../spikes/README.md). The compact live summary still affects Jev: a draft Rule rose from 0.10–0.11 without live data to 0.60–0.62 with it, below its 0.70 threshold, on in-game commentary. One of three chatting repeats was classified `watching_pro` with live metadata. This is recorded, not treated as proof of calibrated live accuracy. Test Rule wording against real live-context Windows before relying on it.

Rejected: dumping every champion/item/player into Whisper; choosing an arbitrary concurrent match; fabricating bans; making a Pack outage stop capture. Revisit after an actual pro match verifies the feed clock and Mode/Rule quality, and when Dota exposes which interface choices need to change.

## D27. Independent Pack refresh and declared Categories (issue #6 review)

2026-10-12. Supersedes D26's selection, refresh-failure, and cache details. The Pack Protocol declares `categories`. The CLI builds a generic Category selector from one Pack factory registry and derives `PACK_NOT_LIVE` warnings from that registry. Adding a Pack requires one registry entry. The Gate's Pack scope comes only from the Window's stored Segment; the current-Pack callback is removed.

Live context and Vocabulary refresh independently with separate timeouts. A live-feed failure clears the previous Game key while catalogue and roster Vocabulary remain available. A Vocabulary failure leaves validated live context available to the Gate. Category revisions still discard obsolete refresh results.

The 24-hour disk cache validates expired copies too. A failed fetch or invalid response serves a usable stale copy with a warning and leaves its timestamp unchanged, so the next refresh retries. Without a usable copy the error propagates to the optional Pack refresh. Catalogue parsing is memoized by parser and file mtime, so changed files invalidate the memo. Roster parsing skips players with missing or empty names. Team and catalogue title matching share whole-word matching.

The Secrets policy explicitly permits the public lolesports website key as a commented, named constant with an environment override. User API keys remain outside the repository. The measured effect of live picks on the draft Rule remains a limit to monitor through Labels in #5; the Gate wording and recordings are unchanged.

## D28. JSON LLM for Extraction, Novelty and the Gate Fallback (issue #5)

2026-10-12. Supersedes D7's Haiku assignments for Extraction, Novelty and the Gate Fallback. There is no Anthropic key yet. Use Groq-hosted `openai/gpt-oss-120b` through the provider-neutral `LLM.complete_json` interface. Notes and Q&A keep their separate model decisions.

The adapter uses JSON object mode, temperature 0, `reasoning_effort=low` and `include_reasoning=false`. The [Groq model page](https://console.groq.com/docs/model/openai/gpt-oss-120b) lists $0.15 per million input tokens and $0.60 per million output tokens. One `PRICE` constant records list price even on the free tier. Hidden reasoning tokens count as completion tokens. The Budget estimate includes the maximum completion tokens; every billed malformed attempt and possibly billed transport attempt reaches the ledger, including when the next attempt fails authentication.

Extraction and Fallback share one adapter and `GROQ_API_KEY`, with rate limits separate from Whisper. JSON calls reserve in-flight quota under a lock, release the lock during HTTP, and reconcile response counts conservatively. The Fallback reserves the last 150 known daily requests for Extraction. Cached counts expire at the provider's request reset deadline. Extraction gives retries 60 seconds; Fallback gives them 20 seconds. JSON or schema errors get one retry. Quotes absent from the normalized transcript are discarded.

One call extracts a claim, a phone summary and an optional quote, and compares the claim with the previous delivered or possibly delivered claim. It uses `gate_decision.question` and the saved `gate_window.state`; it never reinterprets a stored decision using an edited Rule. Extraction explains an implicit take rather than acting as another classifier. The prototype missed the fixture's implicit restated winner prediction, so the prompt now compares the favored side across explicit predictions and statements that its opponent cannot fight it. This measured behavior is pinned by canonical request replays.

Revisit when an Anthropic key is available: implement another JSON provider, repeat the measurements, and compare quality and cost before switching. Fallback probabilities are not calibrated to Jev's thresholds; see the comparison in spikes/README.md.

## D29. Durable Alerts and Telegram Labels (issue #5)

2026-10-12. Narrows D14: Cooldown is per Rule and Channel; Novelty is per Rule, Channel and Game key. A shared Rule must not let one streamer silence another. Use the Pack key when present. Otherwise a coarse key follows the stored Segment and starts a new chain after a gap longer than 20 minutes. Cooldown and duplicate rows extend that chain but do not restart Cooldown.

One `alert` row explains every passing Gate decision. Its unique decision id is the deduplication key, its content is the Novelty reference, and its id is the Label button target. There is no separate attempt or cursor table. A process-wide worker handles decisions on its claimed channels, oldest speech first, including final Windows gated outside a live session. Gate commits wake it; a 10-second poll is the backstop. It runs independently of Gate and transcription tasks.

Delivery is at most once. This can lose an alert when a process dies after committing `sending` but before the Bot API receives it. Telegram has no idempotency key, so reliable retry and guaranteed absence of duplicate messages cannot both be promised. Commit `sending` before sending. Restart recovery, Telegram 5xx, uncertain transport failure and malformed success all become `unconfirmed`, and are never resent. Retry only definite connect failures and valid Bot API 429 responses. Pace sends to one per second to the chat. A send in progress may drain for 15 seconds on shutdown.

Freshness uses the capture leg holding the Window's last sample, including reconnect discontinuities. No leg means stale. The initial 10-minute limit is a value to tune from measured live latency, not a measured latency guarantee. Check it before Extraction and again before delivery. Cooldown uses the speech start. OverBudget or failed Extraction sends a degraded transcript excerpt; it does not hide Gate false positives from Labels.

`👍` means the Rule applied to that decision's `new`; `👎` means it did not. Callbacks name the alert id. Lock that row, verify the existing external id when present, upsert one Label per decision and source, then acknowledge the callback and show its selection. An unconfirmed send becomes `sent` in the same transaction, with the callback's chat/message id and message date. Redelivery is idempotent; a later opposite tap changes the verdict. The advisory lock gives the bot one poller across watch processes. Contenders wait and take ownership when the owner stops; database connection failures retry rather than permanently disabling Labels.

Rejected: inline sends in GateLoop, a per-session alert worker that misses closed-session Windows, Rule-global Cooldown, a second classifier veto, and extra attempt/cursor tables. Bug recovery marks an affected decision failed before send, or unconfirmed after send starts, then continues; an inability to write that recovery stops Alerts for the process. Known limits include model nondeterminism, coarse keys that can combine adjacent games, and the crash gap between a billed LLM response and its transaction. Live runs and offline acceptance results are recorded in spikes/README.md.


## D30. Authentication recovery and bounded Alert shutdown (issue #5 review)

2026-10-12. Supersedes D29's per-send shutdown drain and callback-date details. Clarifies D24's primary-auth stop behavior and D20's Fallback bypass.

A primary Jev authentication error stops gating for the session, but never consumes the Window. Possibly billed costs use a separate session ledger transaction. A nullable unique idempotency key on each standalone cost makes database retries safe after a lost commit acknowledgement. Restarting with a fixed key gates the original transcript. Fallback authentication failure disables only the Fallback for the process, logs its auth error once, and fails the affected Window with combined costs. Each later Window still tries Jev.

Shutdown first stops channels and drains transcription and their final Gate passes. The process-wide worker then gets up to 20 seconds to drain fresh decisions. It and the tap poller are cancelled afterwards. A second signal interrupts either drain. Pending sends interrupted by cancellation keep the existing at-most-once recovery behavior.

A send needs at least five seconds remaining before its deadline, plus the pacing wait. Insufficient time produces `stale` without sending or starting Cooldown. A started send gets its normal 15-second request timeout; staleness gates the start. Callbacks with a missing message date or `date: 0` confirm uncertain delivery at receipt time instead of 1970.

Rejected: consuming a Window to persist primary-auth costs, stopping the primary for a Fallback key error, and cancelling the worker before final Gate decisions exist. Regression coverage includes a real ffmpeg shutdown replay, second-signal interruption, lost ledger acknowledgements, deadline pacing, and inaccessible callbacks.


## D31. Rolling Notes use Groq, with Pack schemas and durable attempt records

2026-10-13. Supersedes D7's Notes model assignment. Notes use Groq `openai/gpt-oss-120b` through the shared `LLM.complete_json` interface. An Anthropic key is not available. Pydantic v2 validates the Pack's versioned schema, then the parser checks every timestamp against the source span. The adapter retries a malformed result once.

Notes tile the session audio clock in 180-second spans, including a closed session's final partial span. They wait for the transcript watermark and, when enabled, Gate progress. Mode is the most common answered Window Mode in the span, with ties going to the latest. The Segment at the span start selects the Pack. Unknown Mode, unsupported Modes, and Packs without Notes use the core `general` schema.

One worker serves the process's claimed channels. It reads stored history, handles open sessions and sessions closed within 12 hours, and resumes from the last terminal Note. It never waits on the capture path. Transcript and Gate commits wake it, with a 30-second poll backstop. Notes reserve 300 requests for Extraction and Fallback. At one Fallback every 45 seconds, that reserve covers roughly 3.75 hours of Gate outage before counting Extraction. This is an initial operating margin, not a measured traffic forecast.

Shutdown drains channels and Alerts, then gives an in-flight Notes request 35 seconds to finish its 30-second provider deadline and write. Notes stop admitting new spans when that drain starts. Cancellation preserves completed attempts in the retry journal and charges an estimate for an interrupted request with an uncertain result. Storage gets at most one extra second after cancellation. A second signal can interrupt storage. A sustained database outage or hard kill can lose an attempt before its journal commits; the pending span is retried. Pending spans resume on restart.

A terminal `note` row and that call's Cost ledger rows commit in one transaction, keyed by `(session_id, start_ms)`. Unavailable or auth failures leave the span pending. A small `note_retry` journal stores those attempts and their ledger rows in its own idempotent transaction. This extends the issue's proposed storage: without a journal, a restart would lose billed pending attempts. Completion copies the attempts into the Note and deletes the journal entries atomically. Every attempt retains its original input, system prompt, model, raw output, and validation error, even if a restart changes the selected schema. JSON export streams terminal rows.

The prompt carries teams, game number, and draft from previous Notes. It forbids copying previous timestamped items. The first live prototype demonstrated why: gpt-oss sometimes repeated previous takes with timestamps outside the new span. Validation rejected those results and the worker continued. The final recording and measured limits are in `spikes/README.md`.

**Rejected:** running Notes inside the Gate loop, which would let a slow completion stall the Gate. A generic agent is unnecessary for this fixed summarization step.

**Revisit:** the model, quota reserve, and prompt after measurements from live streams with reliable Speaker maps.


## D32. Notes retry without a speech deadline, using one durable span row

2026-10-13. Supersedes D31's retry journal, Gate barrier and previous-Note continuity rules.

Notes are not freshness-sensitive Alerts. `LLMUnavailable` leaves a pending span indefinitely;
old speech is not a reason to discard it. This includes quota admission refusals that dispatch
nothing. A process-wide 30-second exponential backoff, capped at 15 minutes, limits provider
and database work. A known provider pause or daily-request reset can extend that backoff.
Authentication stops Notes for the process; bad output still retries once then becomes invalid.
Bugs and budget refusals retain their distinct terminal outcomes. We do not invent an automatic
retry-attempt limit without evidence that a temporary provider outage is a permanent span failure.
The 12-hour filter selects new historical sessions only; once a session has a Note, its backlog
continues across the cutoff, including after restart.

Pending attempts and their costs now commit in `note` itself. Its pending status does not advance
the terminal watermark. Receipt UUIDs make paid pending writes idempotent across lost commit
acknowledgements. Refusals with no attempts or costs need no receipt UUID, and terminal writes
clear pending receipts. Terminal status makes final writes idempotent. This removes `note_retry` and
its copy/delete transaction. The owning row stores the initial input and system once; attempts
refer to those columns implicitly and store overrides only when the request changed. Export
includes the system column, preserving the full dataset without repeating the prompt each time.
The Notes migration is still unmerged, so this changes that migration rather than shipping a
second migration to remove an unreleased table.

A span becomes due when its transcript is complete and the Gate covers it, or the transcript is
270 seconds beyond its end (two 45-second Windows plus three minutes). Closed tails wait at most
five wall-clock minutes after session close. Auth and bug exits explicitly release that session's
barrier for the process. Mode and live context still come from available historical Windows;
unknown Mode selects general. Previous continuity requires at most a ten-minute speech gap and
the same Pack, Segment, Mode and, when both are known, Game key.

General v2 includes timestamped winner predictions so no-Pack co-streams can represent forecasts.
League playing v2 also accepts predictions to match the global writing contract.
League watching_pro v2 describes favorites, odds and implicit loser statements in the JSON schema
as well as the writing instructions. Prediction recall is measured against a committed annotation
list; objective plans, reported results and conditional comeback paths are documented separately.

**Rejected:** expiring old speech after one provider refusal; treating database failure as provider
unavailability; storing duplicate request bodies in every output; permanently blocking Notes on a
Gate process that has stopped.

**Revisit:** a finite attempt policy only if repeated real attempts prove a permanent span problem;
model quality with reliable Speaker maps and more annotated games.
