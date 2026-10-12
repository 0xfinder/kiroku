"""The `kiroku` command line, and the composition root.

This is the only module that builds components from Settings, asks for their secrets, and
imports kiroku.packs (ruff TID251 is waived only here). Commands are sync Typer functions
that run one coroutine each.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

import httpx
import typer

from kiroku import backfill, sessions
from kiroku.budget import Budget
from kiroku.config import SecretName, Settings
from kiroku.db import connect, open_pool
from kiroku.db.migrate import migrate, new_migration
from kiroku.errors import KirokuError
from kiroku.gate.jev import JevGate
from kiroku.sessions import claim_channels
from kiroku.source import StreamlinkSource, ToolMissing
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import vocabulary, watch

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


def _log_to_stderr() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request URL at INFO; keep the log about kiroku.
    logging.getLogger("httpx").setLevel(logging.WARNING)


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
    _log_to_stderr()
    with _user_errors():
        # Capture needs both, and sealing a crashed leg needs ffprobe before a leg is placed.
        # Fail at launch rather than when a stream first goes live.
        for tool in ("ffmpeg", "ffprobe"):
            if shutil.which(tool) is None:
                raise ToolMissing(f"{tool} not found; install it or fix PATH")

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


@app.command("backfill")
def backfill_command(
    channel: Annotated[str, typer.Argument(help="Twitch login, e.g. gorgc")],
    session: Annotated[
        int | None, typer.Option(help="Session id. Default: the channel's latest session.")
    ] = None,
) -> None:
    """Transcribe the gaps a crash left in a session's transcript, from its audio archive.

    `kiroku watch` does this by itself when it resumes a session. Use this for a session it
    won't resume, e.g. one whose stream ended while kiroku was down. Safe beside a running
    `kiroku watch`: it yields to live audio on the same key, leaves legs a watcher may be
    capturing alone, and refuses only while another backfill of the session runs.
    """
    _log_to_stderr()

    async def run(settings: Settings) -> None:
        key = settings.secrets.require(SecretName.GROQ)
        async with (
            open_pool(settings.database_url) as pool,
            httpx.AsyncClient(timeout=60) as http,
        ):
            found_channel = await sessions.find_channel(pool, "twitch", channel)
            found = (
                await sessions.find_session(pool, found_channel, session) if found_channel else None
            )
            if found_channel is None or found is None:
                which = f"session {session}" if session is not None else "session"
                raise KirokuError(f"no {which} found for {channel}")
            transcriber = GroqTranscriber(http, key)
            data_dir = settings.data_dir
            await backfill.seal_open_legs(
                pool, found.id, transcriber.name, data_dir, unless_watched=found_channel
            )
            _report(found.id, await backfill.gaps(pool, found.id, transcriber.name))
            title = found.title
            guard = Budget(pool, settings.caps).guard(found.id)
            if not await backfill.run(
                pool, found.id, transcriber, data_dir, lambda: vocabulary(title, None), guard
            ):
                raise KirokuError(f"a backfill of session {found.id} is already running")
            _report(found.id, await backfill.gaps(pool, found.id, transcriber.name))

    with _user_errors(), contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(run(Settings.load()))


def _report(session_id: int, gaps: list[backfill.Gap]) -> None:
    seconds = sum(g.duration_ms for g in gaps) / 1000
    typer.echo(f"session {session_id}: {len(gaps)} gaps, {seconds:.1f} s untranscribed")


def main() -> None:
    app()
