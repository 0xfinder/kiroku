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
