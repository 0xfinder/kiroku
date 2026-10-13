# Plan

kiroku transcribes live streams, keeps structured notes, sends alerts for topics and events you care about, and answers questions about what a streamer said. The first channels are Caedrel (League of Legends) and Gorgc (Dota 2).

Decisions and their reasons are in [docs/decisions.md](docs/decisions.md). Terms are defined in [CONTEXT.md](CONTEXT.md).

## Pipeline

```
Twitch watcher (streamlink metadata poll ~60 s: live? category?)
  → Source: streamlink → ffmpeg ─┬→ Opus archive (14 days)
                                 └→ PCM → Muse streaming STT (Pack Vocabulary biasing)
  → Utterances (speaker labels) → Speaker map (Jev) → Streamer turns
  → Windows (~45 s) ─┬→ Gate (Jev: Mode + every active Rule) → Extraction + Novelty check (Haiku)
                     │      → Notifier (Telegram 👍/👎) → Labels
                     └→ Notes every 3 min (Groq gpt-oss, versioned Pack schema for Mode)
  → Postgres (transcript, Notes, Gate decisions, Alerts, Labels, Cost ledger)
  → FastAPI (SSE) → SvelteKit web app; Telegram /ask → Q&A agent
```

## Storage (Postgres)

| Table | Holds |
| --- | --- |
| `channel` | Watch list: platform, login, enabled |
| `stream_session` | One live broadcast: start/end times, audio path |
| `segment` | Category/title boundaries on the session audio clock, plus the Pack; end derived from the next boundary |
| `capture_leg` | One streamlink→ffmpeg run: position on the session audio clock, wall-clock start, archive file |
| `utterance` | Transcript text, session-relative and wall-clock times, Speaker label, Transcriber used |
| `transcript_span` | Audio ranges a Transcriber has finalized; the exactly-once key and the completeness watermark |
| `speaker_map` | Speaker label → role, probability, who decided it and when |
| `gate_window` | Window bounds, the state the Gate saw, status, inferred Mode and its probability, Game key |
| `note` | Terminal Notes spans: validated JSONB body, Pack, Mode, schema name and version, model, Game key, exact input and every raw attempt |
| `note_retry` | Pending Notes attempts and original inputs until terminal completion; billed attempts commit with their ledger rows |
| `rule` | Owner, instructions, Pack scope, threshold, Cooldown, enabled |
| `gate_decision` | Window × Rule: the question asked, probability, threshold, passed |
| `alert` | Rule, Game key, content, Notifier, external message id, sent time |
| `label` | Gate decision → yes/no, source (`telegram` or `review`) |
| `cost_ledger` | Provider, model, purpose, tokens or audio seconds, USD, latency |

Added later: a full-text search column on `utterance` and `note`, and a pgvector embeddings table (Phase 5).

Audio files are stored at `audio/<channel>/<session>/<leg>.opus` (D23); evaluation clips at `clips/`.

## Phases

Each phase ends with something that can be observed working.

### Phase 0 — Spikes (time-boxed)

- Muse: stream a recorded Caedrel co-stream clip through it. Record the speaker-label format, the limit on biasing keywords, and the reconnect behaviour.
- ~~Jev: send three Rules plus a Mode question against a transcript Window, and confirm the response shape and latency from this host.~~ Done 2026-10-11 with Groq on a live Gorgc co-stream; see [spikes/README.md](spikes/README.md).
- ~~Source: run streamlink → ffmpeg against a live channel on this host, and measure CPU and RAM use.~~ Done 2026-10-11: 9% of one CPU, 65 MB.

### Phase 1 — Core + League v1

- Twitch watcher, Source, Muse Transcriber (Groq Fallback), audio archive, Backfill of crash gaps from the archive.
- Speaker map, Windows, Mode, and the Gate with every Rule in one Jev call.
- Extraction, Novelty check, and Cooldown; Telegram Notifier with 👍/👎.
- League Vocabulary and `get_live_match` from the `live-lol-esports` lolesports client are implemented (issue #6). Recorded Caedrel name audit: 15 errors with title-only, 9 with match Vocabulary. Teams and picks reach the Gate in replay; the live feed still needs verification during a pro match. The public endpoints do not expose bans.
- Rolling Notes with League schemas for `watching_pro`, `playing`, and `chatting`, plus core `general` Notes, are implemented in issue #7. Notes resume after restart and export the distillation dataset.
- Cost ledger and Budget caps.
- Minimal web app: live transcript, Notes timeline, Rule editor, Review queue.
- **Done when:** during a live Caedrel co-stream, a "predicts the winner" Rule alerts within about 60 s with his reasoning, sends no duplicate for the same game, and the session's cost is visible.

### Phase 2 — Measurement

- Labelled evaluation set from saved audio.
- Muse vs Groq Whisper error rate on League terms.
- Speaker-map error rate.
- Precision and recall per Rule, and threshold tuning.

### Phase 3 — Dota Pack (Gorgc)

- Dota Pack: Vocabulary from OpenDota constants, Notes schemas.
- Dota live-data spike (time-boxed): find Gorgc's game with `GetTopLiveGame`, then check what `GetRealtimeStats` returns. Pro games via `GetLiveLeagueGames`.
- Extract the Pack interface, and record in decisions.md which core changes Dota forced.

### Phase 4 — Q&A agent

- Hand-written tool loop (Sonnet) with `get_live_match`, `get_segment`, `search_notes`. Telegram `/ask`, and web chat that shows each tool call.
- Port to LangGraph and write the comparison.

### Phase 5 — Archive search

- Hybrid search (pgvector + full-text search + filters) and a `search_archive` tool.
- Embedding-based fuzzy topic Rules.

### Phase 6 — Distillation

- Distil the Notes step into a small open model and compare quality, cost, and latency against Haiku.

### Later

YouTube Source, Discord Notifier, offline pyannote speaker pass, `get_stats` from senrigan.
