# Kiroku

Kiroku transcribes live streams (Twitch first), keeps structured notes, sends alerts for topics and events the user cares about, and answers questions about what a streamer said. The first channels are Caedrel (League of Legends) and Gorgc (Dota 2).

## Current status

Phase 1 is in progress. In place: the scaffold (CLI, config, Postgres migrations, cost ledger, CI), the live transcript (`kiroku watch`), crash backfill (`kiroku backfill`, also automatic when a session resumes), the Gate (Windows, Jev scoring of Rules, Budget caps), Rule management (`kiroku rule add|edit|list|enable|disable|test`), the League Pack (Vocabulary, live context, Segment history), Telegram Alerts (Extraction, Novelty, Labels, Gate Fallback), and rolling Notes (Pack schemas, timeline, distillation export). Read these before starting work:

- [PLAN.md](PLAN.md): the pipeline, storage, and phases.
- [docs/decisions.md](docs/decisions.md): what was decided, why, and what was rejected.
- [CONTEXT.md](CONTEXT.md): the domain glossary. Use these terms in code and docs.
- [spikes/README.md](spikes/README.md): measured results that shaped the design (Gate context split, Rule wording, Vocabulary).

## Setup and checks

```sh
docker compose up -d --wait     # Postgres 17 + pgvector on 127.0.0.1:5432
uv sync
uv run kiroku db migrate        # idempotent; needs no API keys
uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest
```

- CI runs the same four checks against the same compose file. Run all four before pushing.
- Tests need the compose Postgres and fail (not skip) when it is down. Each test gets its own database cloned from a migrated template, so tests may commit.
- New migration: `uv run kiroku db new <name>`, then write SQL in the file it prints. Never edit a migration that has been merged; the runner refuses changed checksums.
- Tests also need `ffmpeg` on PATH; the end-to-end tests run real ffmpeg on synthetic audio (`tests/fakes/`).
- `kiroku watch` sends Telegram Alerts by default; `--no-alerts` runs the Gate and its Fallback without sending or polling Telegram. `--no-gate` skips the Gate and Alerts, and Notes use unknown Mode with the core `general` schema. `--no-notes` disables Notes. Combine `--no-gate --no-notes` to transcribe only. `kiroku alert list --last N` shows delivered and suppressed outcomes.
- Run a live session with `KIROKU_DATA_DIR=/tmp/x .venv/bin/kiroku watch <channel>`. It needs `GROQ_API_KEY`, `JEV_API_KEY`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. With `--no-alerts` or `--no-gate`, Telegram credentials are optional. Budget caps come from `KIROKU_SESSION_CAP_USD` (default 1.50) and `KIROKU_MONTH_CAP_USD` (default 20). Don't use `uv run` when you need a clean stop: `uv run` ends the child during Ctrl-C before it drains the transcript. systemd and the bare binary drain correctly on SIGTERM and SIGINT.
- Notes requests are held to `spikes/results/2026-10-13-notes.jsonl`. Re-record with `uv run python spikes/notes_replay.py spikes/results/2026-10-13-notes.jsonl --database <new scratch name>` after changing the prompt, input layout, schemas, or pinned model. Previous timestamped items are context only; never copy them into a new span. `kiroku notes list [--session ID]` reads the timeline, and `kiroku notes export --since YYYY-MM-DD` streams the dataset.
- Groq Extraction and Fallback requests are held to the canonical bodies in `spikes/results/2026-10-12-alert-extraction.jsonl` and `2026-10-12-gate-fallback.jsonl`. Re-record with the matching spike after changing a prompt, input layout or model. Live spikes use isolated migrated databases; never migrate the dev database for a measurement.
- Changing the Gate's wording, its `state` layout or the pinned Jev model fails `tests/test_gate_replay.py` until you re-run `uv run python spikes/gate_split.py ...` (see spikes/README.md) and commit the new recording. This is deliberate: CI holds the Gate to what was measured live.
- After a crash, `kiroku watch` backfills the session's transcript gaps from the archive when it resumes the session. For a session it won't resume, run `KIROKU_DATA_DIR=... .venv/bin/kiroku backfill <channel> [--session N]`. It is safe beside a running watch. It refuses only while another backfill of the session runs.
- Read an Opus archive's length with `kiroku.archive.decoded_ms`, not ffprobe's `duration`. The `duration` field overstates it by the 6.5 ms pre-skip.
- After killing an asyncio subprocess whose stdout you stopped reading, reap it with `communicate()`, not `wait()`. On Python 3.12, `wait()` also waits for stdout to close, and a paused pipe never closes.
- Channel claims explicitly unlock their dedicated Postgres connection before closing it. A socket close can return before Postgres releases advisory locks; immediate Backfill or watch handoff must wait for the unlock acknowledgement. Suppress `psycopg.Error` during unlock so a broken connection cannot replace the original exception; socket closure still releases its locks.
- Try a Rule's wording with `kiroku rule test <name> [--session ID | --last N] [--instructions "..."]` before relying on it. It makes real Jev calls against stored Windows (about $0.00003 each, counted against the monthly cap) and stores no decisions. Naming concrete events beats abstract categories (spikes/README.md, 2026-10-12).
- If `docker` says permission denied right after Docker was installed, the session predates the `docker` group. Run commands as `sg docker -c "docker compose up -d --wait"`.
- League uses a 24-hour cache at `<data_dir>/packs/league`. The lolesports website key is public; override it with `KIROKU_LOLESPORTS_API_KEY`, not secrets.env. Data Dragon includes unnamed internal items; skip those names, and validate API shapes before caching them.
- Expired League cache data remains usable after a failed refresh; warn and retry without renewing its timestamp. Parsed catalogue data is memoized by file mtime. Live context and Vocabulary refresh independently, so a draft-feed outage clears the Game key but preserves catalogue and roster Vocabulary.
- League Gate requests with `context.live` are held to `spikes/results/2026-10-12-league-gate.jsonl`. Re-record with `spikes/league_gate.py` after changing that layout. Live metadata can affect Mode and Rule scores; see the measured limits in spikes/README.md.

## Workflow

- Use Conventional Commits for commits and PR titles, e.g. `feat(alerts): Telegram Alerts`.

- Work is tracked in GitHub issues on `0xfinder/kiroku`. Issues exist only for the current phase; PLAN.md holds the roadmap.
- Each issue gets one branch and one PR, squash-merged. Reference the issue in the PR with `Closes #N`.
- A PR is a vertical slice that runs and shows something. Don't split a feature into "models" and "logic" PRs.
- Run `/code-review` on every PR before asking the user to merge. Use pstack `interrogate` on risky ones. The user merges.
- Parallel agents work in separate worktrees, on issues that don't depend on each other's unmerged work.

## Code guide

- Python 3.12, managed with `uv`. The web front end is SvelteKit, served by a FastAPI backend.
- The core must not import from any Pack. Game-specific knowledge lives in `packs/<game>`. The composition root `cli.py` imports Packs and injects a Category selector; Ruff enforces this exception.
- Segment boundaries use the captured audio clock, including queued transcription. Gate recovery reads the Segment at each Window's start. Never assign current live match context or a current Game key to a historical backlog.
- A crash can leave a Segment beyond the resumed audio clock. Reconcile those future boundaries before new capture. Keep a failed metadata write's original boundary and retry it before the next probe, even if the Category subsequently reverts; the Gate must wait past an unpersisted boundary.
- Notes are the lowest-priority Groq JSON consumer and preserve 300 requests for Extraction and Fallback. Pending attempts and their ledger rows live in the same `note` row as the terminal result. Use receipt UUIDs only when storing attempts or costs, and clear them on completion. Provider unavailability never expires by speech age; retry with backoff and honor known provider pauses/resets. Started backlogs remain eligible beyond the history cutoff.
- Notes wait for the Gate for at most a 270-second transcript margin or five minutes after session close. A stopped Gate releases Notes immediately. Previous Notes require recent speech (within ten minutes) and matching Pack, Segment, Mode and, when both exist, Game key.
- Shielded Notes writes must re-raise cancellation even if storage fails during the grace period; never admit a later paid span after shutdown cancellation.
- Shutdown stops channels and drains transcription and the final Gate pass before giving Alerts a bounded drain. A second signal interrupts both drains. On Python 3.12, TaskGroup does not forward a second cancellation to children already stopping; the watch supervisor uses tracked gather tasks for that reason.
- Primary Gate auth costs belong in a separate idempotent ledger transaction; leave the Window ungated for a restart with a fixed key. Fallback auth disables only the Fallback for the process.
- Alert delivery is at most once. Commit `sending` before the Bot API call; a 5xx, uncertain transport result or malformed success becomes `unconfirmed` and is never resent. A valid tap can confirm delivery in the Label transaction.
- Alert freshness uses the capture leg holding the Window's end, not its close time or its first leg. Extraction uses the stored Gate question and state; later Rule edits and disablement do not change a passed decision.
- Cooldown is per Rule and Channel; Novelty is per Rule, Channel and Game key. Suppressed rows extend the coarse Game chain but never restart Cooldown.
- Groq JSON calls share quota admission and rate-limit pauses. Expire cached request counts at the provider reset time; otherwise a Gate Fallback can remain refused after the daily quota resets.
- Every paid call (transcription, Jev, LLM) goes through the provider interface and writes a Cost ledger row.
- The alert pipeline is a fixed sequence of steps, not an agent (D5). Agents are reserved for the Q&A path.
- Tests replay recorded audio and transcript fixtures (e.g. `spikes/results/`), never live streams. Write E2E tests for observable behaviour, including failure paths.

## Secrets

- API keys live in `~/.config/kiroku/secrets.env` (mode 600, outside the repo), as `JEV_API_KEY`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Muse (Meta) and Anthropic keys are still pending.
- Never log Telegram httpx exceptions or Request objects; the bot token is in the URL. Log the exception type only, and sanitize token and chat ID in recordings.
- Never put user API keys or other credentials in project files or commit messages, and never echo them into logs.
- Public, non-user keys that a vendor ships to every browser, including the lolesports website key, may be committed as named constants with a comment identifying their public origin. Keep the environment override for key rotation. This exception never applies to user API keys.

## Keep these instructions current

- When a task reveals a verified, reusable lesson, update this file in the same PR.
- When a decision changes, add a new entry to docs/decisions.md that supersedes the old one. Don't rewrite history.

## Web app

- Run Python and web checks sequentially on the 2-vCPU, 3.7-GB host. Concurrent type checkers can exhaust memory and stall in direct reclaim. CI keeps these steps sequential too. For SvelteKit 3, extend `$app/tsconfig`; it loads generated ambient types through `compilerOptions.types = ["$app/types"]`. Explicit source `include` and generated-output `exclude` lists are valid. Do not switch to the stale SvelteKit 2 `.svelte-kit/tsconfig.json`.
- Build the static Svelte app with `cd web && npm ci && npm run check && npm run test && npm run build`, then run `uv run kiroku serve`. It binds `127.0.0.1:8737` with one worker and needs no API keys until a paid Rule test. Shutdown bounds open SSE connections to five seconds. The build is ignored by Git, built in CI, and included when packaging after a build. Production runs no Node server.
- D18: expose the app only through Tailscale or Cloudflare Access. A non-loopback `--host` is opt-in and logs a D18 warning. The app has no account system.
- Web reads use table-owner APIs; Review writes and undo use `labels`, Rule edits use `rules`, and paid tests use `dry_run`. Web tests bound both recent and session-specific selectors to at most 100 Windows.
- When a Rule edit omits `enabled`, preserve the current database value in the UPDATE. Writing a prior read's value can undo a concurrent CLI or web disablement.
- Run `cd web && npx playwright install chromium && npm run e2e` for browser acceptance. It creates and drops its own migrated Postgres database, commits synthetic speech through `transcript.commit`, checks Review labels, and saves phone/desktop screenshots under `docs/web/screenshots/`. Never point this runner at the dev database.
- An SSE audio-clock ID groups all Utterances at that offset. It cannot describe a late Backfill behind that clock. Reconcile stored transcript pages on reconnect and from the earliest affected offset after a reconciliation hint; deduplicate by Utterance ID. Initial paging uses a fixed commit-ID snapshot and SSE subscribes from it. Transcript commits lock the session row before allocating IDs; the indexed commit cursor depends on per-session visibility following ID allocation order.
- Paid web Rule tests return 202 and a job ID; poll `/api/rule-tests/{id}` for progress and the terminal report or error. One test runs at a time. Its result stays in process memory until the next test starts, and shutdown cancels the running task within five seconds.
