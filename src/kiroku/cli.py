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
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Annotated

import httpx
import typer

from kiroku import dry_run, rules
from kiroku.budget import Budget
from kiroku.config import SecretName, Settings
from kiroku.db import Pool, connect, open_pool
from kiroku.db.migrate import migrate, new_migration
from kiroku.errors import KirokuError
from kiroku.gate import rule_question
from kiroku.gate.jev import JevGate
from kiroku.packs.league import LeaguePack
from kiroku.packs.league.data import PUBLIC_WEBSITE_KEY
from kiroku.rules import Rule
from kiroku.sessions import claim_channels
from kiroku.source import StreamlinkSource
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import watch

app = typer.Typer(no_args_is_help=True, help="Transcribe live streams, keep Notes, send Alerts.")
db_app = typer.Typer(no_args_is_help=True, help="Database schema.")
app.add_typer(db_app, name="db")
rule_app = typer.Typer(no_args_is_help=True, help="Alert Rules: what the Gate asks about.")
app.add_typer(rule_app, name="rule")


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
            league = LeaguePack(
                http,
                settings.data_dir,
                settings.lolesports_api_key or PUBLIC_WEBSITE_KEY,
            )
            await watch(
                pool,
                sources,
                GroqTranscriber(http, groq_key),
                settings.data_dir,
                budget=Budget(pool, settings.caps),
                gate=JevGate(http, jev_key) if jev_key else None,
                poll_s=poll_s,
                select_pack=lambda category: league if category == "League of Legends" else None,
            )

    with _user_errors(), contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        try:
            asyncio.run(run(Settings.load()))
        except* KirokuError as group:
            # One channel's TaskGroup failure arrives as a group; report the first.
            raise group.exceptions[0] from None


# ---- rules ---------------------------------------------------------------------------------


def _threshold(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise typer.BadParameter(f"{value!r} is not a number, e.g. 0.7") from None


Threshold = Annotated[
    Decimal | None,
    typer.Option(parser=_threshold, help="Pass at this probability or above, e.g. 0.7."),
]


def _run_with_pool[T](body: Callable[[Pool], Awaitable[T]]) -> T:
    async def run(settings: Settings) -> T:
        async with open_pool(settings.database_url) as pool:
            return await body(pool)

    with _user_errors():
        return asyncio.run(run(Settings.load()))


def _show(rule: Rule) -> None:
    state = "enabled" if rule.enabled else "disabled"
    pack = rule.pack or "any pack"
    typer.echo(
        f"{rule.name} ({state}, threshold {rule.threshold}, {pack}, cooldown {rule.cooldown_s} s)"
    )
    typer.echo(f"  {rule.instructions}")


@rule_app.command("add")
def rule_add(
    name: Annotated[str, typer.Argument(help="Short id, e.g. predicts_winner.")],
    instructions: Annotated[str, typer.Argument(help="The yes/no question, about what is said.")],
    threshold: Threshold = Decimal("0.7"),
    cooldown_s: Annotated[int, typer.Option(help="Minimum seconds between Alerts.")] = 600,
    pack: Annotated[str | None, typer.Option(help="Only for this Pack, e.g. league.")] = None,
) -> None:
    """Add a Rule. Try it with `kiroku rule test` before relying on it."""
    assert threshold is not None
    _show(
        _run_with_pool(
            lambda pool: rules.add(
                pool, name, instructions, threshold=threshold, cooldown_s=cooldown_s, pack=pack
            )
        )
    )


@rule_app.command("edit")
def rule_edit(
    name: str,
    instructions: Annotated[str | None, typer.Option(help="New question wording.")] = None,
    threshold: Threshold = None,
    cooldown_s: Annotated[int | None, typer.Option()] = None,
    pack: Annotated[str | None, typer.Option(help="Only for this Pack.")] = None,
    any_pack: Annotated[bool, typer.Option("--any-pack", help="Apply with any Pack.")] = False,
) -> None:
    """Change a Rule. Past decisions keep the wording they were asked with."""
    _show(
        _run_with_pool(
            lambda pool: rules.edit(
                pool,
                name,
                instructions=instructions,
                threshold=threshold,
                cooldown_s=cooldown_s,
                pack=pack,
                clear_pack=any_pack,
            )
        )
    )


@rule_app.command("enable")
def rule_enable(name: str) -> None:
    """Ask this Rule in live Gate requests again."""
    _show(_run_with_pool(lambda pool: rules.set_enabled(pool, name, True)))


@rule_app.command("disable")
def rule_disable(name: str) -> None:
    """Stop asking this Rule. Its past decisions stay."""
    _show(_run_with_pool(lambda pool: rules.set_enabled(pool, name, False)))


@rule_app.command("list")
def rule_list() -> None:
    """Every Rule, enabled or not."""
    found = _run_with_pool(rules.all_rules)
    for rule in found:
        _show(rule)
    if not found:
        typer.echo("no rules yet; add one with `kiroku rule add`")


@rule_app.command("test")
def rule_test(
    name: str,
    session: Annotated[int | None, typer.Option(help="Every Window of this session.")] = None,
    last: Annotated[int, typer.Option(min=1, help="Otherwise, the latest N Windows.")] = 40,
    instructions: Annotated[
        str | None, typer.Option(help="Try this wording instead, without saving it.")
    ] = None,
    threshold: Threshold = None,
) -> None:
    """Ask the Gate this Rule about stored Windows and show how the scores spread.

    Real Jev calls: they cost money (about $0.00003 each) and count against the monthly
    Budget cap. With --instructions, NAME may be a Rule that doesn't exist yet.
    """

    async def body(pool: Pool) -> str:
        settings = Settings.load()
        key = settings.secrets.require(SecretName.JEV)
        try:
            saved: Rule | None = await rules.get(pool, name)
        except rules.RuleError:
            if instructions is None:
                raise
            saved = None
        if instructions is None:
            assert saved is not None
            rule = saved
        else:
            # An unsaved rewording, compared at the saved Rule's threshold unless told
            # otherwise, so the two runs differ only in wording.
            rule = Rule(
                0, name, instructions.strip(), saved.threshold if saved else Decimal("0.7"), None, 0
            )
        if threshold is not None:
            rule = replace(rule, threshold=threshold)
        rules.check(rule.name, rule.instructions, rule.threshold)
        windows = await dry_run.stored_windows(pool, session_id=session, last=last)
        if not windows:
            raise KirokuError("no stored Windows with speech yet; run `kiroku watch` first")
        question = rule_question(rule)
        async with httpx.AsyncClient() as http:
            guard = Budget(pool, settings.caps).guard(None)
            trials = await dry_run.run(pool, JevGate(http, key), guard, question, windows)
        return dry_run.report(name, question, trials)

    typer.echo(_run_with_pool(body))


def main() -> None:
    app()
