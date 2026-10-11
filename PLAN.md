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
                     └→ Notes every ~3 min (Haiku, Pack schema for the current Mode)
  → Postgres (transcript, Notes, Gate decisions, Alerts, Labels, Cost ledger)
  → FastAPI (SSE) → SvelteKit web app; Telegram /ask → Q&A agent
```

## Storage (Postgres)

| Table | Holds |
| --- | --- |
| `channel` | Watch list: platform, login, enabled |
| `stream_session` | One live broadcast: start/end times, audio path |
| `segment` | Category spans within a session, plus the Pack |
| `utterance` | Transcript text, session-relative and wall-clock times, Speaker label, Transcriber used |
| `speaker_map` | Speaker label → role, probability, who decided it and when |
| `window` | Window bounds, inferred Mode and its probability |
| `note` | JSONB body, Pack, Mode, `schema_version`, model, time span, Game key |
| `rule` | Owner, instructions, Pack scope, threshold, Cooldown, enabled |
| `gate_decision` | Window × Rule: probability, threshold, passed, model version |
| `alert` | Rule, Game key, content, Notifier, external message id, sent time |
| `label` | Gate decision → yes/no, source (`telegram` or `review`) |
| `cost_ledger` | Provider, model, purpose, tokens or audio seconds, USD, latency |

Added later: a full-text search column on `utterance` and `note`, and a pgvector embeddings table (Phase 5).

Audio files are stored at `audio/<channel>/<session>.opus`; evaluation clips at `clips/`.

## Phases

Each phase ends with something that can be observed working.

### Phase 0 — Spikes (time-boxed)

- Muse: stream a recorded Caedrel co-stream clip through it. Record the speaker-label format, the limit on biasing keywords, and the reconnect behaviour.
- Jev: send three Rules plus a Mode question against a hand-picked transcript Window, and confirm the response shape and latency from this host.
- Source: run streamlink → ffmpeg → PCM against a live channel on this host, and measure CPU and RAM use.

### Phase 1 — Core + League v1

- Twitch watcher, Source, Muse Transcriber (Groq Fallback), audio archive.
- Speaker map, Windows, Mode, and the Gate with every Rule in one Jev call.
- Extraction, Novelty check, and Cooldown; Telegram Notifier with 👍/👎.
- Notes on a schedule using the League Pack: Vocabulary, Notes schemas, and `get_live_match` from the `live-lol-esports` lolesports client.
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
