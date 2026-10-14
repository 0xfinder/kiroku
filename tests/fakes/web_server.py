"""Playwright's disposable Postgres/server lifecycle and observable fixture actions."""

import argparse
import asyncio
import json
import os
import signal
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import psycopg
import uvicorn
from psycopg import sql

from kiroku import transcript
from kiroku.db import connect, open_pool
from kiroku.db.migrate import migrate
from kiroku.transcriber import Result, Utterance
from kiroku.web import app as web_app
from kiroku.web import stream as web_stream
from tests.conftest import ADMIN_URL, url_for
from tests.fakes.oidc import config, provider_server, tls_files
from tests.fakes.web import TRANSCRIBER, seed
from tests.test_web import settings

STATE = Path(".web-e2e.json")
stream_epoch = 0


def disconnect_streams(_signum: int, _frame: Any) -> None:
    global stream_epoch
    stream_epoch += 1


original_events = web_app.events


async def disconnectable_events(*args: Any, **kwargs: Any) -> AsyncGenerator[str]:
    epoch = stream_epoch
    async with aclosing(original_events(*args, **kwargs)) as source:
        async for event in source:
            if epoch != stream_epoch:
                return
            yield event


async def prepare(name: str) -> tuple[int, int]:
    url = url_for(name)
    async with connect(url) as conn:
        await migrate(conn)
    async with open_pool(url) as pool:
        return await seed(pool, url)


async def append(
    name: str, session: int, *, backfill: bool, second: bool = False, third: bool = False
) -> None:
    async with open_pool(url_for(name)) as pool:
        start = (3000 if third else 2000 if second else 1000) if backfill else 180000
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
                        (
                            "Offline Backfill appeared"
                            if third
                            else "Second Backfill appeared"
                            if second
                            else "Historical Backfill appeared"
                        )
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
        choices=[
            "serve",
            "append",
            "backfill",
            "backfill-second",
            "backfill-third",
            "disconnect",
            "labels",
        ],
        default="serve",
    )
    args = parser.parse_args()
    if args.action != "serve":
        state = json.loads(STATE.read_text())
        if args.action == "disconnect":
            os.kill(state["pid"], signal.SIGUSR1)
        elif args.action == "labels":
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
                    third=args.action == "backfill-third",
                )
            )
        return
    name = f"kiroku_web_e2e_{os.getpid()}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            session, decision = asyncio.run(prepare(name))
            STATE.write_text(
                json.dumps(
                    {"name": name, "session": session, "decision": decision, "pid": os.getpid()}
                )
            )
            # A signal closes real SSE responses so Chromium exercises native reconnects.
            signal.signal(signal.SIGUSR1, disconnect_streams)
            web_stream.POLL_S = 0.1
            web_stream.KEEPALIVE_S = 0.2
            web_app.events = disconnectable_events
            # Real HTTPS cookies and OIDC against a local provider; no production keys.
            with provider_server() as fake, TemporaryDirectory() as tls_dir:
                cert, key = tls_files(Path(tls_dir))
                cfg = replace(settings(url_for(name)), oidc=config("https://127.0.0.1:8748"))
                uvicorn.run(
                    web_app.create_app(cfg, provider=fake.provider),
                    host="127.0.0.1",
                    port=8748,
                    ssl_certfile=cert,
                    ssl_keyfile=key,
                    proxy_headers=False,
                    access_log=False,
                    timeout_graceful_shutdown=5,
                )
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            STATE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
