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
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

import httpx
import typer

from kiroku import alerting, backfill, dry_run, notes, rules, sessions
from kiroku.budget import Budget
from kiroku.config import ConfigError, SecretName, Settings
from kiroku.db import Pool, connect, open_pool
from kiroku.db.migrate import migrate, new_migration
from kiroku.errors import KirokuError
from kiroku.gate import rule_question
from kiroku.gate.fallback import FallbackGate
from kiroku.gate.jev import JevGate
from kiroku.gate.llm import LLMGate
from kiroku.llm.groq import GroqLLM
from kiroku.notify.telegram import TelegramNotifier
from kiroku.pack import Pack, pack_selector
from kiroku.packs.league import LeaguePack
from kiroku.packs.league.data import PUBLIC_WEBSITE_KEY
from kiroku.rules import Rule
from kiroku.sessions import claim_channels
from kiroku.source import StreamlinkSource, ToolMissing
from kiroku.transcriber.groq import GroqTranscriber
from kiroku.watch import vocabulary, watch

app = typer.Typer(no_args_is_help=True, help="Transcribe live streams, keep Notes, send Alerts.")
db_app = typer.Typer(no_args_is_help=True, help="Database schema.")
app.add_typer(db_app, name="db")
rule_app = typer.Typer(no_args_is_help=True, help="Alert Rules: what the Gate asks about.")
app.add_typer(rule_app, name="rule")
alert_app = typer.Typer(no_args_is_help=True, help="Recent Alerts and suppression reasons.")
app.add_typer(alert_app, name="alert")
notes_app = typer.Typer(no_args_is_help=True, help="Rolling Notes timeline and dataset.")
app.add_typer(notes_app, name="notes")

PACK_FACTORIES: dict[str, Callable[[httpx.AsyncClient, Settings], Pack]] = {
    LeaguePack.name: lambda http, settings: LeaguePack(
        http, settings.data_dir, settings.lolesports_api_key or PUBLIC_WEBSITE_KEY
    ),
}


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
        bool, typer.Option("--no-gate", help="Skip the Gate; needs no JEV_API_KEY.")
    ] = False,
    no_notes: Annotated[bool, typer.Option("--no-notes", help="Skip rolling Notes.")] = False,
    no_alerts: Annotated[
        bool, typer.Option("--no-alerts", help="Gate without Telegram Alerts or Labels.")
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
        telegram_key = (
            None if no_alerts or no_gate else settings.secrets.require(SecretName.TELEGRAM)
        )
        chat_id = None
        if telegram_key is not None:
            try:
                chat_id = int(settings.secrets.require(SecretName.TELEGRAM_CHAT).reveal())
            except ValueError:
                raise ConfigError("TELEGRAM_CHAT_ID must be an integer") from None
        async with (
            open_pool(settings.database_url) as pool,  # refuses a stale schema first
            claim_channels(settings.database_url, "twitch", channels) as claimed,
            httpx.AsyncClient(timeout=60) as http,
        ):
            sources = [(c, StreamlinkSource(c.login)) for c in claimed]
            packs = [factory(http, settings) for factory in PACK_FACTORIES.values()]
            budget = Budget(pool, settings.caps)
            llm = GroqLLM(http, groq_key)
            transcriber = GroqTranscriber(http, groq_key)
            notifier = (
                TelegramNotifier(http, telegram_key, chat_id)
                if telegram_key is not None and chat_id is not None
                else None
            )
            alerts = (
                alerting.AlertWorker(pool, [c.id for c in claimed], llm, notifier, budget)
                if notifier is not None
                else None
            )
            await watch(
                pool,
                sources,
                transcriber,
                settings.data_dir,
                budget=budget,
                gate=FallbackGate(JevGate(http, jev_key, give_up_s=20), LLMGate(llm))
                if jev_key
                else None,
                alerts=alerts,
                notes=notes.NotesWorker(
                    pool,
                    [c.id for c in claimed],
                    transcriber.name,
                    llm,
                    budget,
                    packs,
                    gated=not no_gate,
                )
                if not no_notes
                else None,
                notifier=notifier,
                database_url=settings.database_url,
                poll_s=poll_s,
                select_pack=pack_selector(packs),
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
    if rule.pack is not None and rule.pack not in PACK_FACTORIES:
        typer.echo(
            f"PACK_NOT_LIVE: {rule.pack} is unavailable; "
            "this Rule won't be asked by the live Gate.",
            err=True,
        )


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


@alert_app.command("list")
def alert_list(
    last: Annotated[int, typer.Option(min=1, help="Number of recent Alerts.")] = 20,
) -> None:
    """Show delivery and suppression outcomes, newest first."""

    async def run(settings: Settings) -> None:
        async with open_pool(settings.database_url) as pool:
            for said_at, channel, rule, outcome, reason, claim in await alerting.recent(pool, last):
                typer.echo(f"{said_at or '-'} {channel} {rule} {outcome} {reason or claim or ''}")

    with _user_errors():
        asyncio.run(run(Settings.load()))


@notes_app.command("list")
def notes_list(
    session: Annotated[int | None, typer.Option(help="Session id.")] = None,
    last: Annotated[int, typer.Option(min=1, help="Number of spans.")] = 20,
) -> None:
    """Print structured Notes as a timestamped timeline."""

    async def run() -> None:
        async with open_pool(Settings.load().database_url) as pool:
            for row in await notes.rows(pool, session_id=session, last=last):
                typer.echo(notes.timeline(row))

    with _user_errors():
        asyncio.run(run())


@notes_app.command("export")
def notes_export(
    since: Annotated[str, typer.Option(help="Inclusive UTC date, YYYY-MM-DD.")],
) -> None:
    """Write one distillation input/output pair per Notes span as JSONL."""

    async def run(date: datetime) -> None:
        async with open_pool(Settings.load().database_url) as pool:
            async for row in notes.export(pool, date):
                typer.echo(notes.canonical(row))

    with _user_errors():
        try:
            date = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            raise KirokuError("--since must be a date in YYYY-MM-DD format") from None
        asyncio.run(run(date))
