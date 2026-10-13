# Web app boundaries

`kiroku serve` starts one Uvicorn worker. FastAPI owns one query pool and a notification
hub with one dedicated LISTEN connection. SvelteKit builds an SPA into the Python package;
production runs no Node process. The API calls table owners for every read and write.

Session cards combine `sessions.browse`, `segments.latest`, `gating.latest_mode`,
`notes.count_for_session` and `alerting.count_for_session`. Transcript pages use
`transcript.page`; Notes and alerts use focused timeline reads. Gate owns the borderline
Review query and session passes. Labels owns Review upsert and undo, scoped by source.
Rules keep their existing validation. Dry runs add a bounded selection and structured
report shared with the CLI; the web process admits one paid test at a time.

The stream subscribes before reading. Notifications wake readers after commit; a five-second
poll covers listener outages. Forward transcript events use the audio clock as their SSE ID.
An audio offset represents a group of utterances, including comparisons by multiple
transcribers. A reconciliation event refreshes historical rows after commits, and the
browser reconciles on connection open too. Time-only IDs cannot describe late Backfill.

The design comparison favored the shared hub for its fixed database connection count.
The per-request listener candidate supplied subscribe-before-read and historical
reconciliation. Both candidates identified unbounded session-specific dry runs, pending
Notes omitted by the CLI read, and the need for a source-scoped undo operation.
