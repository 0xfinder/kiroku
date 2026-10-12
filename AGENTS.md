# Kiroku

Kiroku transcribes live streams (Twitch first), keeps structured notes, sends alerts for topics and events the user cares about, and answers questions about what a streamer said. The first channels are Caedrel (League of Legends) and Gorgc (Dota 2).

## Current status

Phase 1 is in progress. In place: the scaffold (CLI, config, Postgres migrations, cost ledger, CI), the live transcript (`kiroku watch`), the Gate (Windows, Jev scoring of Rules, Budget caps), Rule management (`kiroku rule add|edit|list|enable|disable|test`), and the League Pack (Vocabulary, live context, Segment history). Read these before starting work:

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
- Run a live session with `KIROKU_DATA_DIR=/tmp/x .venv/bin/kiroku watch <channel>`. It needs `GROQ_API_KEY` and `JEV_API_KEY`. Budget caps come from `KIROKU_SESSION_CAP_USD` (default 1.50) and `KIROKU_MONTH_CAP_USD` (default 20). Don't use `uv run` when you need a clean stop: `uv run` ends the child during Ctrl-C before it drains the transcript. systemd and the bare binary drain correctly on SIGTERM and SIGINT.
- Changing the Gate's wording, its `state` layout or the pinned Jev model fails `tests/test_gate_replay.py` until you re-run `uv run python spikes/gate_split.py ...` (see spikes/README.md) and commit the new recording. This is deliberate: CI holds the Gate to what was measured live.
- Try a Rule's wording with `kiroku rule test <name> [--session ID | --last N] [--instructions "..."]` before relying on it. It makes real Jev calls against stored Windows (about $0.00003 each, counted against the monthly cap) and stores no decisions. Naming concrete events beats abstract categories (spikes/README.md, 2026-10-12).
- If `docker` says permission denied right after Docker was installed, the session predates the `docker` group. Run commands as `sg docker -c "docker compose up -d --wait"`.
- League uses a 24-hour cache at `<data_dir>/packs/league`. The lolesports website key is public; override it with `KIROKU_LOLESPORTS_API_KEY`, not secrets.env. Data Dragon includes unnamed internal items; skip those names, and validate API shapes before caching them.
- Expired League cache data remains usable after a failed refresh; warn and retry without renewing its timestamp. Parsed catalogue data is memoized by file mtime. Live context and Vocabulary refresh independently, so a draft-feed outage clears the Game key but preserves catalogue and roster Vocabulary.
- League Gate requests with `context.live` are held to `spikes/results/2026-10-12-league-gate.jsonl`. Re-record with `spikes/league_gate.py` after changing that layout. Live metadata can affect Mode and Rule scores; see the measured limits in spikes/README.md.

## Workflow

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
- Every paid call (transcription, Jev, LLM) goes through the provider interface and writes a Cost ledger row.
- The alert pipeline is a fixed sequence of steps, not an agent (D5). Agents are reserved for the Q&A path.
- Tests replay recorded audio and transcript fixtures (e.g. `spikes/results/`), never live streams. Write E2E tests for observable behaviour, including failure paths.

## Secrets

- API keys live in `~/.config/kiroku/secrets.env` (mode 600, outside the repo), as `JEV_API_KEY` and `GROQ_API_KEY`. Muse (Meta) and Anthropic keys are still pending.
- Never put user API keys or other credentials in project files or commit messages, and never echo them into logs.
- Public, non-user keys that a vendor ships to every browser, including the lolesports website key, may be committed as named constants with a comment identifying their public origin. Keep the environment override for key rotation. This exception never applies to user API keys.

## Keep these instructions current

- When a task reveals a verified, reusable lesson, update this file in the same PR.
- When a decision changes, add a new entry to docs/decisions.md that supersedes the old one. Don't rewrite history.
