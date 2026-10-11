"""The `kiroku` command line, and the composition root.

This is the only module that builds components from Settings, asks for their secrets, and
imports kiroku.packs (ruff TID251 is waived only here). Commands are sync Typer functions
that run one coroutine each.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import typer

from kiroku.config import Settings
from kiroku.db import connect
from kiroku.db.migrate import migrate, new_migration
from kiroku.errors import KirokuError

app = typer.Typer(no_args_is_help=True, help="Transcribe live streams, keep Notes, send Alerts.")
db_app = typer.Typer(no_args_is_help=True, help="Database schema.")
app.add_typer(db_app, name="db")


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a command's coroutine. Expected failures print one line and exit 2."""
    try:
        return asyncio.run(coro)
    except KirokuError as e:
        typer.echo(f"kiroku: {e}", err=True)
        raise typer.Exit(2) from None


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending migrations. Safe to repeat; needs no API keys."""
    settings = Settings.load()

    async def run() -> None:
        async with connect(settings.database_url) as conn:
            applied = await migrate(conn)
        for m in applied:
            typer.echo(f"applied {m}")
        if not applied:
            typer.echo("schema is up to date")

    _run(run())


@db_app.command("new")
def db_new(name: str) -> None:
    """Create an empty migration file named with the current UTC time."""
    try:
        path = new_migration(name)
    except KirokuError as e:
        typer.echo(f"kiroku: {e}", err=True)
        raise typer.Exit(2) from None
    typer.echo(str(path))


def main() -> None:
    app()
