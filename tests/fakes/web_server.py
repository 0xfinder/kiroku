"""Playwright's disposable Postgres/server lifecycle and observable fixture actions."""

import argparse
import asyncio
import json
import os
import signal
from pathlib import Path

import psycopg
from psycopg import sql

from kiroku import transcript
from kiroku.cli import app
from kiroku.db import connect, open_pool
from kiroku.db.migrate import migrate
from kiroku.transcriber import Result, Utterance
from tests.conftest import ADMIN_URL, url_for
from tests.fakes.web import TRANSCRIBER, seed

STATE = Path(".web-e2e.json")


async def prepare(name: str) -> tuple[int, int]:
    url = url_for(name)
    async with connect(url) as conn:
        await migrate(conn)
    async with open_pool(url) as pool:
        return await seed(pool, url)


async def append(name: str, session: int, *, backfill: bool, second: bool = False) -> None:
    async with open_pool(url_for(name)) as pool:
        start = (2000 if second else 1000) if backfill else 180000
        await transcript.commit(
            pool,
            session,
            "fixture:backfill" if backfill else TRANSCRIBER,
            Result(
                start,
                start + 5000,
                (
                    Utterance(
                        start,
                        start + 4000,
                        ("Second Backfill appeared" if second else "Historical Backfill appeared")
                        if backfill
                        else "Live speech arrived without a reload",
                    ),
                ),
                None,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=["serve", "append", "backfill", "backfill-second", "labels"],
        default="serve",
    )
    args = parser.parse_args()
    if args.action != "serve":
        state = json.loads(STATE.read_text())
        if args.action == "labels":
            with psycopg.connect(url_for(state["name"]), autocommit=True) as conn:
                rows = conn.execute(
                    "SELECT applies,source FROM label WHERE decision_id=%s ORDER BY source",
                    [state["decision"]],
                ).fetchall()
                print(json.dumps(rows))
        else:
            asyncio.run(
                append(
                    state["name"],
                    state["session"],
                    backfill=args.action.startswith("backfill"),
                    second=args.action == "backfill-second",
                )
            )
        return
    name = f"kiroku_web_e2e_{os.getpid()}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            session, decision = asyncio.run(prepare(name))
            STATE.write_text(json.dumps({"name": name, "session": session, "decision": decision}))
            # This is the production CLI, with a test-only database and no API keys.
            os.environ["KIROKU_DATABASE_URL"] = url_for(name)
            os.environ["KIROKU_SECRETS_FILE"] = str(Path("tests/absent-web-secrets.env").resolve())
            os.environ.pop("JEV_API_KEY", None)
            signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
            signal.signal(signal.SIGINT, lambda _signum, _frame: None)
            app(args=["serve", "--port", "8748"], standalone_mode=False)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
