# Using kiroku

kiroku runs on a single Linux host. It is built for one user and has no account system. Terms such as Rule, Window and Pack are defined in [CONTEXT.md](../CONTEXT.md).

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Docker, for Postgres 17 with pgvector
- ffmpeg on `PATH`
- Node 22 and npm, only to build the web app

## Setup

```sh
docker compose up -d --wait        # Postgres on 127.0.0.1:5432
uv sync
source .venv/bin/activate
kiroku db migrate                  # idempotent; needs no API keys
```

Put API keys in `~/.config/kiroku/secrets.env`, outside the repo, with mode 600:

```sh
GROQ_API_KEY=...          # transcription, Notes, Extraction, Gate fallback
JEV_API_KEY=...           # the Gate classifier
TELEGRAM_BOT_TOKEN=...    # Alerts
TELEGRAM_CHAT_ID=...
GOOGLE_CLIENT_ID=...      # owner login for writes; read-only without these
GOOGLE_CLIENT_SECRET=...
KIROKU_SESSION_SECRET=... # 32+ random characters, signs sessions and login cookies
```

## Watch channels

```sh
kiroku watch caedrel gorgc
```

kiroku checks each channel about once a minute, and starts capturing and transcribing as soon as one goes live. It runs until stopped. Ctrl-C or SIGTERM finishes transcribing the audio already captured before it exits. Run the binary directly, as above or from systemd: `uv run` stops the child process before it finishes.

| Flag | Effect |
| --- | --- |
| `--no-alerts` | Run the Gate but send nothing to Telegram. Telegram keys become optional. |
| `--no-gate` | Skip the Gate and Alerts. No Jev key is needed. |
| `--no-notes` | Skip rolling Notes. |
| `--no-gate --no-notes` | Transcribe only. |

Budget caps default to $1.50 per session and $20 per month. Set them with `KIROKU_SESSION_CAP_USD` and `KIROKU_MONTH_CAP_USD`. Paid calls stop when a cap is reached.

If kiroku crashed and won't resume a session, fill its transcript gaps from the audio archive:

```sh
kiroku backfill caedrel [--session N]
```

## Rules

A Rule is a yes/no question about what the streamer says. Concrete events work better than abstract categories.

```sh
kiroku rule add predicts_winner "Does the speaker predict which team will win the current game?" \
    --threshold 0.6 --cooldown-s 600
kiroku rule add draft_take "Does the speaker talk about picking or banning champions in this game's draft?" \
    --pack league                                   # League streams only

kiroku rule test predicts_winner --last 40          # score it against the latest 40 stored Windows
kiroku rule test predicts_winner --instructions "Does he say which team is favoured?"   # try a new wording
kiroku rule list
kiroku rule disable draft_take                      # also: rule enable, rule edit
```

`rule test` makes real classifier calls, about $0.00003 each, counted against the monthly cap. It stores no decisions.

## Alerts and Notes

```sh
kiroku alert list --last 20            # sent alerts, and why others were suppressed
kiroku notes list --session 12         # the Notes timeline for a session
kiroku notes export --since 2026-10-01 > notes.jsonl   # the dataset for distillation
```

Tap 👍 or 👎 on a Telegram alert to label it. Labels are stored against the Gate decision and feed precision measurements.

## Web app

```sh
cd web && npm ci && npm run build && cd ..
kiroku serve                           # http://127.0.0.1:8737
```

| Page | What it shows |
| --- | --- |
| Live | Sessions, each with its transcript updating in place |
| Notes | The Notes timeline for a session, with alerts and Gate passes |
| Rules | Edit Rules, and run a paid test against stored Windows (cost shown before it runs) |
| Review | Borderline Gate decisions (probability 0.3–0.7) to label yes or no |

Reads are public; writes need the owner. In production the app runs behind the Cloudflare Tunnel at its HTTPS origin: set `KIROKU_OWNER_EMAIL` and `KIROKU_PUBLIC_URL`, add the Google login secrets above, and register `<public URL>/auth/callback` as the client's redirect URI. Rules, Review labels and paid Rule tests then require the owner's Google login; without login configuration everyone stays read-only, and loopback never grants owner access. `serve` binds to loopback by default, and a non-loopback `--host` is opt-in and logs a warning, so keep it behind Tailscale or the tunnel, never open.

## Development

```sh
uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest
cd web && npm run check && npm run test && npm run build
```

Tests need the compose Postgres and ffmpeg. Each test gets its own database cloned from a migrated template. On a small host, run checks one at a time. [AGENTS.md](../AGENTS.md) has the full contributor guide.
