# Supported change paths

Every invariant below has a named owner, a focused check, and, where a historical
regression was replayed during planning, a registered contract case in `tests/contracts/`.
A contributor changing one of these invariants
receives a failure that names the case, the supported owner and the observed wrong behavior.
A stale mutation, an invalid experiment or an unavailable database fails the check instead of
appearing green.

Run the contract checks with:

```sh
uv run python tests/contracts/check_mutations.py        # self-checks and all cases
uv run python tests/contracts/check_mutations.py --list # what is enforced
```

| Change path | Owner | Invariant | Enforcement |
| --- | --- | --- | --- |
| Jev (Gate) requests | `kiroku.gate.jev.JevGate.evaluate` | Terminal 4xx raises `GateBadResponse` carrying the accumulated estimates of dispatches lost earlier in the same evaluate; retries keep the valid total | `jev-terminal-4xx-keeps-prior-estimates` contract case; `tests/test_paid_exits.py` |
| Whisper (transcription) requests | `kiroku.transcriber.groq.Whispers._call` | Terminal 4xx returns an empty `Result` retaining the lost chunk's estimate; terminal auth raises `TranscriberAuthError` with the same; a retries success keeps the correct total | `whisper-terminal-400...` and `-auth...` cases; `tests/test_paid_exits.py` |
| Primary Gate auth | `kiroku.gating.GateLoop._pass` + `kiroku.ledger.record_session_costs` | An auth error through a terminal failed Window records its estimate and leaves every Window ungated; a restart with a fixed key gates from the start | `gate-auth-error-still-records-its-costs` case; `tests/test_gating.py::test_an_auth_error...` |
| Notes writes (cancellation) | `kiroku.notes.durable` | A crash or cancellation during a notified write pays nothing for unconfirmed spans; a re-enabled/new session starts from the durable span end | `notes-durable-cancellation` case; `tests/test_notes.py::test_failed_write_during_cancellation...` |
| Notes context | `kiroku.notes.prepare` | Previous-note selection never crosses historical Pack, Mode or Game-key boundaries; current live context and Pack never backdate a backlog | `historical-backlog-does-not-inherit-live-context`; `tests/test_notes.py::test_previous_is_recent...` |
| Source subprocesses | `kiroku.source._stop` | PIPE-stdout children reaped by draining; fd-redirected keep wait()'s kill order; teardown stays bounded | `tests/test_source.py` teardown lanes; `tests/test_live_replay.py` shutdown lanes |
| Live transcription backlog | `kiroku.transcriber.groq.GroqTranscriber.transcribe` | Enqueue evicts old PCM during provider pauses, preserving the current retry/newest tail and releasing voiced admission; Backfill never evicts | `live-pcm-is-bounded-during-provider-pause` case; `tests/test_groq.py::test_live_audio_memory_stays_bounded_while_rate_limited` |
| Segments / metadata | `kiroku.segments`, `kiroku.source` | Segment boundaries use the captured audio clock; crash rewrites reconcile before new capture; a metadata retry keeps the original boundary | `tests/test_sessions_transcript.py`, `tests/test_backfill.py` |
| Table-owned writes (web) | `labels`, `rules`, `dry_run` APIs | Review writes and undo use `labels`, Rule edits `rules`, paid tests `dry_run`; a removed owner guard fails the structural test | `tests/test_web_auth.py::test_route_enumeration...`; web browser acceptance (`npm run e2e`) |
| Web reads | `src/kiroku/web/` routers | Public read-only via the tunnel; owner login for writes/private reads; single-worker admission bounds | `tests/test_web_auth.py`, `tests/test_web_security.py`, `tests/test_web.py` |
| Recorded request bodies | `spikes/results/*.jsonl` | Codified Extraction/Fallback/Notes/Gate-League bodies replay: replay tests fail after prompt, input-layout, model, wording or schema edits until re-recorded | `tests/test_alert_replay.py`, `tests/test_gate_replay.py`, `tests/test_notes.py::test_live_fixture_replay` |
| Core/Pack independence | `src/kiroku/packs/`, `tests/packs/` | Core imports never reach a Pack; `kiroku.cli` is the only injector | Ruff `TID251` ban on `kiroku.packs` (`pyproject.toml`), exceptions only `cli.py` and `src/kiroku/packs/**` |
| SQL idempotency | `kiroku.*` writers | Window commits are `ON CONFLICT DO NOTHING`, transcript commits lock the session row and allocate IDs in commit order, alert delivery is at-most-once, lost acknowledges stay idempotent | `tests/test_sessions_transcript.py::test_commit_is_idempotent...`, `test_alert_replay`, `test_ledger.py` |
| Shutdown ordering | `kiroku.watch` | Channels stop, rewrite drains and the final Gate pass before Alerts; a second signal interrupts drains | `tests/test_live_replay.py`, `tests/test_watch_step.py` |

## Adding a case

Register it in `tests/contracts/cases.py` only when all three exist: a passing control on
current code, a textual mutation (or a pinned historical replay, stored verbatim under
`tests/contracts/fixtures/`) that reproduces the historical defect, and a focused test
whose failure IS the intended mismatch. The mutation must apply to a fresh scratch copy;
never to the active checkout; a patch whose source no longer matches fails as stale so no
stale case can silently expire.
