# Kiroku

Kiroku transcribes live streams (Twitch first), keeps structured notes, sends alerts for topics and events the user cares about, and answers questions about what a streamer said. The first channels are Caedrel (League of Legends) and Gorgc (Dota 2).

## Current status

Phase 1 is starting. There is no application code yet, only design docs and the spike. Read these before starting work:

- [PLAN.md](PLAN.md): the pipeline, storage, and phases.
- [docs/decisions.md](docs/decisions.md): what was decided, why, and what was rejected.
- [CONTEXT.md](CONTEXT.md): the domain glossary. Use these terms in code and docs.
- [spikes/README.md](spikes/README.md): measured results that shaped the design (Gate context split, Rule wording, Vocabulary).

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
