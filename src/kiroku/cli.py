"""The `kiroku` command line, and the composition root.

This is the only module that builds components from Settings, asks for their secrets, and
imports kiroku.packs (ruff TID251 is waived only here). Commands are sync Typer functions
that run one coroutine each.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

import httpx
import typer

from kiroku.budget import Budget
from kiroku.config import SecretName, Settings
from kiroku.db import connect, open_pool
from kiroku.db.migrate import migrate, new_migration
from kiroku.errors import KirokuError
from kiroku.gate.jev import JevGate
from kiroku.sessions import claim_channels
from kiroku.source import StreamlinkSource
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import watch

app = typer.Typer(no_args_is_help=True, help="Transcribe live streams, keep Notes, send Alerts.")
db_app = typer.Typer(no_args_is_help=True, help="Database schema.")
app.add_typer(db_app, name="db")


@contextmanager
def _user_errors() -> Generator[None]:
    """Every command body runs inside this. Expected failures print one line and exit 2."""
    try:
        yield
    except KirokuError as e:
        typer.echo(f"kiroku: {e}", err=True)
        raise typer.Exit(2) from None


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending migrations. Safe to repeat; needs no API keys."""

    async def run(database_url: str) -> None:
        async with connect(database_url) as conn:
            applied = await migrate(conn)
        for m in applied:
            typer.echo(f"applied {m}")
        if not applied:
            typer.echo("schema is up to date")

    with _user_errors():
        asyncio.run(run(Settings.load().database_url))


@db_app.command("new")
def db_new(name: str) -> None:
    """Create an empty migration file named with the current UTC time."""
    with _user_errors():
        typer.echo(str(new_migration(name)))


@app.command("watch")
def watch_command(
    channels: Annotated[list[str], typer.Argument(help="Twitch logins, e.g. gorgc caedrel")],
    poll_s: Annotated[float, typer.Option(help="Seconds between live checks.")] = 60.0,
    no_gate: Annotated[
        bool, typer.Option("--no-gate", help="Transcribe only; needs no JEV_API_KEY.")
    ] = False,
) -> None:
    """Watch channels, store a live transcript while they stream, and gate it against the
    active Rules. Runs until stopped."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request URL at INFO; keep the log about kiroku.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def run(settings: Settings) -> None:
        # systemd stops services with SIGTERM. Treat it like Ctrl-C: cancel, so every live
        # session ends its leg cleanly and drains its transcription before exit.
        main = asyncio.current_task()
        assert main is not None
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main.cancel)
        groq_key = settings.secrets.require(SecretName.GROQ)
        jev_key = None if no_gate else settings.secrets.require(SecretName.JEV)
        async with (
            open_pool(settings.database_url) as pool,  # refuses a stale schema first
            claim_channels(settings.database_url, "twitch", channels) as claimed,
            httpx.AsyncClient(timeout=60) as http,
        ):
            sources = [(c, StreamlinkSource(c.login)) for c in claimed]
            await watch(
                pool,
                sources,
                GroqTranscriber(http, groq_key),
                settings.data_dir,
                budget=Budget(pool, settings.caps),
                gate=JevGate(http, jev_key) if jev_key else None,
                poll_s=poll_s,
            )

    with _user_errors(), contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        try:
            asyncio.run(run(Settings.load()))
        except* KirokuError as group:
            # One channel's TaskGroup failure arrives as a group; report the first.
            raise group.exceptions[0] from None


def main() -> None:
    app()
