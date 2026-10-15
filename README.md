<img src="assets/icon.svg" width="64" height="64" alt="">

# kiroku

kiroku listens to esports streams live, writes down what was said, and messages me when something I care about happens.

## Why I built this

I follow a lot of esports: League of Legends, Valorant, CS2 and Dota 2. On a big weekend several of them are live at once, often as co-streams where someone like Caedrel or Gorgc talks over the official broadcast. I can't watch all of them, and the highlights and summaries I find afterwards miss the part I watch co-streams for: the streamer's own reads, predictions and opinions.

So I built something that watches for me. It transcribes the streams I pick, keeps structured notes on each game, and sends me a Telegram message when the streamer does something I asked about, such as "he predicts which team wins this game". I get the information without keeping four streams open, and I decide what counts as important.

It's also how I'm learning to build with LLMs properly: measure before trusting, keep costs bounded, and use an agent only where a plain pipeline won't do.

## What it does

- **Live transcript.** Detects when a channel goes live, captures the audio and transcribes it as it streams. Speech lands in Postgres about 20–30 s after it's said.
- **Alerts from plain-English Rules.** You write a Rule as a yes/no question. Every 45 s of speech is checked against all of your Rules, and a passing one sends a Telegram message with 👍/👎 buttons. Your taps become labelled data.
- **Rolling Notes.** Every 3 minutes, an LLM writes a structured note: teams, draft, key moments, takes and predictions, each tied to a timestamp.
- **Game knowledge.** For League, kiroku pulls champion, item and roster names from Riot's and lolesports' APIs, plus the live match (teams and picks). Dota gets hero, item and title-roster names from OpenDota. Whisper gets those names to spell, and the classifier knows which game is on.
- **Web app.** A live transcript in the browser, the Notes timeline, a Rule editor that can test a Rule against past speech, and a review queue for borderline decisions. It works on a phone, reads are public, and the owner signs in with Google to edit Rules and label Reviews.

## How it works

```
Twitch (streamlink) ──► ffmpeg ──┬──► Opus archive (crash recovery)
                                 └──► silence-cut chunks ──► Groq Whisper ──► transcript (Postgres)
                                                                                  │
          League Pack: Data Dragon + lolesports (names, live match) ──────────────┤
                                                                                  ▼
                                                   45 s Windows ──► Gate: one Jev call scores every Rule
                                                                                  │ passes
                                                                                  ▼
                                                   Extraction + Novelty check (gpt-oss-120b) ──► Telegram
                                                   3 min spans ──► Notes (gpt-oss-120b, Pack schema)
                                                   FastAPI + SSE ──► SvelteKit web app
```

The alert path is a fixed pipeline, not an agent: its steps never change, so an agent would add cost and nondeterminism for nothing. Agents are saved for the Q&A feature, where the steps depend on the question.

## Problems I ran into, and what fixed them

Each of these was settled by an experiment against real stream audio. The scripts and raw results are committed in [`spikes/`](spikes/README.md).

**Whisper can't spell esports.** On a Gorgc stream it heard "Yatoro" as "Yatora" and "Anti-Mage" as "AIM". *Fix:* each game gets a Pack that builds a Vocabulary from live data (champion names, current rosters and the ten players in the match) and passes it to Whisper. On a recorded Caedrel game, errors on player and champion names fell from 15 to 9 out of 36 audited, and player-name errors from 3 to 0.

**The classifier kept re-firing on old speech.** A "predicts the winner" Rule scored a real prediction 0.70, then 0.70 again on the next 30 s chunk, which held no prediction; the earlier sentence was still in its context. *Fix:* split each Window into `context` and `new`, and ask Rules about `new` only. The false positive fell to 0.10–0.13, and both real predictions still passed in every repeat.

**Rule wording matters as much as the threshold.** "Does he give an opinion on the draft?" passed 5 of 8 Windows, including ones about item builds. "Does he talk about picking or banning heroes, such as why a hero was picked or banned?" passed exactly the 3 about the draft. *Fix:* `kiroku rule test` runs a Rule, or a new wording, against stored Windows before you rely on it.

**One prediction shouldn't produce three alerts.** *Fix:* a Cooldown per Rule and channel, and a Novelty check that compares each alert with the last one for the same game. On the recorded Dota game, an implicit restatement of an earlier prediction was caught as a duplicate in 3 of 3 runs.

**Costs have to stay bounded.** *Fix:* two stages. A cheap classifier (about $0.00004 per Window) scores every Rule in one call, and the LLM runs only when a Rule passes. Every paid call writes a cost-ledger row, and hard caps ($1.50 per session, $20 per month) stop paid calls when reached. Transcribing a 6-hour stream costs about $0.24.

**Crashes shouldn't lose speech or double-send.** Audio is archived per capture leg. After a restart, kiroku transcribes whatever the crash missed, behind live audio. Transcript commits are exactly once. Alerts are at most once: a send whose outcome is unknown is never retried.

## Results and limits

What has been measured, on recorded streams:

- Mode detection ("watching a pro game", "playing", "chatting") was right on all 8 chunks of a Dota co-stream.
- 18 of 18 Notes were valid against their schema, and all 132 timestamps fell inside their spans.
- Notes caught 6 of 9 annotated predictions. Caedrel's one was caught in every run. Gorgc's explicit "70–30" call was caught every time, and his implicit one never.

Not done yet:

- **A full live test.** The pipeline now runs live end to end: alerts fired during live games on 14–15 Oct (Caedrel and Gorgc), with repeats suppressed by Cooldown. The last piece, a session-cost view, is still missing.
- **Speaker separation.** Caster commentary is still mixed into the transcript. Speaker labels from a streaming transcriber are planned.
- **More games.** League has a full Pack; Dota has vocabulary (heroes, items and title rosters) but no live match data or its own Notes schema yet. Other games run without a Pack: title-only vocabulary and no live match data.

## Tech stack

| Area | Tools |
| --- | --- |
| Backend | Python 3.12, asyncio, Typer CLI, FastAPI, Pydantic, uv |
| Data | Postgres 17 (pgvector image), async psycopg 3, raw SQL, a small migration runner |
| Audio | streamlink, ffmpeg, Opus archive, silence-based chunking |
| Models | Groq `whisper-large-v3-turbo` (speech), Jev `jev-1.13.0` (the Gate classifier), Groq `gpt-oss-120b` (Extraction, Novelty, Notes, Gate fallback) |
| Front end | SvelteKit (static build), TypeScript, Zod, Server-Sent Events over Postgres `LISTEN/NOTIFY` |
| Alerts | Telegram Bot API |
| Quality | pytest (523 tests), Playwright, Vitest, Pyright strict, Ruff, GitHub Actions |

## How it's built

- **Tests replay real data, never live streams.** End-to-end tests push synthetic audio through real ffmpeg and Postgres. API calls replay recorded request and response pairs, so CI fails if a prompt or request layout drifts from what was measured.
- **Decisions are written down.** [`docs/decisions.md`](docs/decisions.md) has 37 entries: what was chosen, why, what was rejected, and when to revisit. [`CONTEXT.md`](CONTEXT.md) defines the domain terms the code uses.
- **Every feature is a vertical slice.** One issue, one PR, and it has to run and show something.

## Usage

See [docs/usage.md](docs/usage.md) for setup, API keys, commands and the web app. The short version:

```sh
docker compose up -d --wait && uv sync && source .venv/bin/activate
kiroku db migrate
kiroku rule add predicts_winner "Does the speaker predict which team will win the current game?"
kiroku watch caedrel gorgc
```

## Roadmap

1. **Measurement:** a labelled evaluation set, transcription error rates on game terms, and precision and recall per Rule.
2. **Dota Pack:** vocabulary shipped; live match data and its Notes schema next.
3. **Q&A:** ask "what did Caedrel think of the draft in game 2?" from Telegram or the web. It starts as a hand-written tool loop, then gets ported to LangGraph with a written comparison.
4. **Archive search:** hybrid pgvector and full-text search across every stream.
5. **Distillation:** train a small open model on the Notes dataset kiroku already collects, and compare its quality and cost.

The full plan is in [PLAN.md](PLAN.md).
