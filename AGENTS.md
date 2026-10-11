# Kiroku

Kiroku transcribes live streams (Twitch first), keeps structured notes, sends alerts for topics and events the user cares about, and answers questions about what a streamer said. The first channels are Caedrel (League of Legends) and Gorgc (Dota 2).

## Current status

Design only; no code yet. Read these before starting work:

- [PLAN.md](PLAN.md): pipeline, storage, and phases.
- [docs/decisions.md](docs/decisions.md): what was decided, why, and what was rejected.
- [CONTEXT.md](CONTEXT.md): domain glossary. Use these terms in code and docs.

## Code guide

- Python 3.12, managed with `uv`. Web front end is SvelteKit, served by a FastAPI backend.
- The core must not import from any Pack. Game-specific knowledge lives in `packs/<game>`.
- Every paid call (transcription, Jev, LLM) goes through the provider interface and writes a Cost ledger row.
- The alert pipeline is a fixed sequence of steps, not an agent (D5). Agents are reserved for the Q&A path.
- Write E2E tests for observable behaviour, including failure paths. Use recorded audio and transcript fixtures rather than live streams.

## Keep these instructions current

- When a task reveals a verified, reusable lesson, update this file in the same change.
- When a decision changes, add a new entry to docs/decisions.md that supersedes the old one. Don't rewrite history.
- Never put credentials or API keys in project files.
