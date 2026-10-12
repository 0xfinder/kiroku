import asyncio
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from kiroku.cli import app
from kiroku.db import connect
from kiroku.db.migrate import (
    AppliedMigration,
    Migration,
    MigrationDrift,
    MigrationError,
    load_migrations,
    migrate,
    new_migration,
    pending,
)

runner = CliRunner()
CORE_TABLES = {"channel", "stream_session", "utterance", "cost_ledger"}


def test_migrate_twice_via_cli_needs_no_api_keys(blank_database_url: str) -> None:
    env = {"KIROKU_DATABASE_URL": blank_database_url, "KIROKU_SECRETS_FILE": "/nonexistent"}

    first = runner.invoke(app, ["db", "migrate"], env=env)
    assert first.exit_code == 0, first.output
    assert "applied 20261011120000_core" in first.output

    with psycopg.connect(blank_database_url) as c:
        rows = c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    assert {r[0] for r in rows} >= CORE_TABLES

    second = runner.invoke(app, ["db", "migrate"], env=env)
    assert second.exit_code == 0, second.output
    assert second.output.strip() == "schema is up to date"


def test_unreachable_database_is_a_one_line_error() -> None:
    env = {
        "KIROKU_DATABASE_URL": "postgresql://kiroku@127.0.0.1:1/x?connect_timeout=2",
        "KIROKU_SECRETS_FILE": "/nonexistent",
    }
    result = runner.invoke(app, ["db", "migrate"], env=env)
    assert result.exit_code == 2
    assert result.stderr.startswith("kiroku: cannot connect to Postgres at 127.0.0.1:1: ")
    assert len(result.stderr.strip().splitlines()) == 1
    assert "Traceback" not in result.output


async def test_concurrent_migrate_applies_once(blank_database_url: str) -> None:
    async def run() -> list[Migration]:
        async with connect(blank_database_url) as conn:
            return await migrate(conn)

    results = await asyncio.gather(run(), run())
    assert sorted(len(r) for r in results) == [0, len(load_migrations())]


def test_edited_applied_migration_is_refused() -> None:
    applied = [AppliedMigration("20260101000000", Migration("20260101000000", "a", "old").checksum)]
    with pytest.raises(MigrationDrift):
        pending([Migration("20260101000000", "a", "new")], applied)


def test_unapplied_older_migration_still_runs() -> None:
    # A PR merged late can carry a timestamp older than the newest applied migration.
    old, new = Migration("20260101000000", "a", "x"), Migration("20260201000000", "b", "y")
    assert pending([old, new], [AppliedMigration(new.version, new.checksum)]) == [old]


def test_database_ahead_of_code_is_refused() -> None:
    with pytest.raises(MigrationError, match="this code lacks"):
        pending([], [AppliedMigration("20260101000000", "x")])


def test_new_migration_is_named_to_load(tmp_path: Path) -> None:
    path = new_migration("add_rule", datetime(2026, 10, 12, 9, 30, tzinfo=UTC), tmp_path)
    assert path.name == "20261012093000_add_rule.sql"
    with pytest.raises(MigrationError):
        new_migration("Bad Name", directory=tmp_path)


def test_insecure_secrets_file_is_a_one_line_error(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets.env"
    secrets.write_text("GROQ_API_KEY=x\n")
    secrets.chmod(0o644)
    result = runner.invoke(app, ["db", "migrate"], env={"KIROKU_SECRETS_FILE": str(secrets)})
    assert result.exit_code == 2
    assert "chmod 600" in result.stderr
    assert "Traceback" not in result.output


def test_wrong_database_names_the_real_cause(template_db: str) -> None:
    from tests.conftest import url_for

    env = {"KIROKU_DATABASE_URL": url_for("kiroku_missing_db"), "KIROKU_SECRETS_FILE": "/x"}
    result = runner.invoke(app, ["db", "migrate"], env=env)
    assert result.exit_code == 2
    assert 'database "kiroku_missing_db" does not exist' in result.stderr


async def test_migrate_refuses_non_autocommit_connection(blank_database_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(blank_database_url) as conn:
        with pytest.raises(ValueError, match="autocommit"):
            await migrate(conn)


def test_watch_on_an_unmigrated_database_says_to_migrate(blank_database_url: str) -> None:
    env = {
        "KIROKU_DATABASE_URL": blank_database_url,
        "KIROKU_SECRETS_FILE": "/x",
        "GROQ_API_KEY": "k",
    }
    result = runner.invoke(app, ["watch", "gorgc", "--no-gate"], env=env)
    assert result.exit_code == 2
    assert "run `kiroku db migrate`" in result.stderr
