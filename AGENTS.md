# Kiroku

Kiroku transcribes live streams (Twitch first), keeps structured notes, sends alerts for topics and events the user cares about, and answers questions about what a streamer said. The first channels are Caedrel (League of Legends) and Gorgc (Dota 2).

## Current status

Phase 1 is in progress: the scaffold (CLI, config, Postgres migrations, cost ledger, CI), the live transcript (`kiroku watch`), and crash backfill (`kiroku backfill`, also automatic when a session resumes) are in place. Read these before starting work:

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
- Run a live session with `KIROKU_DATA_DIR=/tmp/x .venv/bin/kiroku watch <channel>`. Don't use `uv run` when you need a clean stop: `uv run` ends the child during Ctrl-C before it drains the transcript. systemd and the bare binary drain correctly on SIGTERM and SIGINT.
- After a crash, `kiroku watch` backfills the session's transcript gaps from the archive when it resumes the session. For a session it won't resume, run `KIROKU_DATA_DIR=... .venv/bin/kiroku backfill <channel> [--session N]`. It refuses while a watcher holds the channel.
- Read an Opus archive's length with `kiroku.archive.decoded_ms`, not ffprobe's `duration`. The `duration` field overstates it by the 6.5 ms pre-skip.
- After killing an asyncio subprocess whose stdout you stopped reading, reap it with `communicate()`, not `wait()`. On Python 3.12, `wait()` also waits for stdout to close, and a paused pipe never closes.
- If `docker` says permission denied right after Docker was installed, the session predates the `docker` group. Run commands as `sg docker -c "docker compose up -d --wait"`.

## Workflow

- Work is tracked in GitHub issues on `0xfinder/kiroku`. Issues exist only for the current phase; PLAN.md holds the roadmap.
- Each issue gets one branch and one PR, squash-merged. Reference the issue in the PR with `Closes #N`.
- A PR is a vertical slice that runs and shows something. Don't split a feature into "models" and "logic" PRs.
- Run `/code-review` on every PR before asking the user to merge. Use pstack `interrogate` on risky ones. The user merges.
- Parallel agents work in separate worktrees, on issues that don't depend on each other's unmerged work.

## Code guide

- Python 3.12, managed with `uv`. The web front end is SvelteKit, served by a FastAPI backend.
- The core must not import from any Pack. Game-specific knowledge lives in `packs/<game>`.
- Every paid call (transcription, Jev, LLM) goes through the provider interface and writes a Cost ledger row.
- The alert pipeline is a fixed sequence of steps, not an agent (D5). Agents are reserved for the Q&A path.
- Tests replay recorded audio and transcript fixtures (e.g. `spikes/results/`), never live streams. Write E2E tests for observable behaviour, including failure paths.

## Secrets

- API keys live in `~/.config/kiroku/secrets.env` (mode 600, outside the repo), as `JEV_API_KEY` and `GROQ_API_KEY`. Muse (Meta) and Anthropic keys are still pending.
- Never put credentials in project files or commit messages, and never echo them into logs.

## Keep these instructions current

- When a task reveals a verified, reusable lesson, update this file in the same PR.
- When a decision changes, add a new entry to docs/decisions.md that supersedes the old one. Don't rewrite history.
