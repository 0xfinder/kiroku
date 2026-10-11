"""Forward-only SQL migrations, applied once each.

Files are `<UTC yyyymmddHHMMSS>_<name>.sql`, created with `kiroku db new <name>`. Timestamps
rather than counters, so two PRs written in parallel never claim the same version. Any file
not yet applied runs, in filename order, even if it sorts before the newest applied one.

- Each file runs in one transaction with its `schema_migration` row; a failure applies nothing.
- An advisory lock serializes concurrent runs. The loser waits, then finds nothing pending.
- Applied files are checksummed. Editing one raises MigrationDrift: fix forward with a new file.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from psycopg import AsyncConnection

from kiroku.errors import KirokuError

ADVISORY_LOCK_KEY = 0x6B69726F6B75  # "kiroku"
FILENAME = re.compile(r"^(?P<version>\d{14})_(?P<name>[a-z0-9_]+)\.sql$")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class MigrationError(KirokuError):
    pass


class MigrationDrift(MigrationError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()

    def __str__(self) -> str:
        return f"{self.version}_{self.name}"


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: str
    checksum: str


def load_migrations() -> list[Migration]:
    """Read the packaged .sql files, so this works from an installed wheel too."""
    found: dict[str, Migration] = {}
    for entry in (resources.files("kiroku.db") / "migrations").iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = FILENAME.match(entry.name)
        if match is None:
            raise MigrationError(f"bad migration filename {entry.name}; use `kiroku db new`")
        version = match["version"]
        if version in found:
            raise MigrationError(f"two migrations share version {version}")
        found[version] = Migration(version, match["name"], entry.read_text())
    return [found[v] for v in sorted(found)]


def pending(available: list[Migration], applied: list[AppliedMigration]) -> list[Migration]:
    """Which migrations still need to run. Pure, so it is tested without a database."""
    by_version = {m.version: m for m in available}
    for a in applied:
        m = by_version.get(a.version)
        if m is None:
            raise MigrationError(f"database has migration {a.version}, which this code lacks")
        if m.checksum != a.checksum:
            raise MigrationDrift(f"{m}.sql was edited after it was applied; add a new migration")
    done = {a.version for a in applied}
    return [m for m in available if m.version not in done]


async def read_applied(conn: AsyncConnection) -> list[AppliedMigration]:
    """Migrations recorded in the database; [] for a database never migrated."""
    cur = await conn.execute("SELECT to_regclass('schema_migration') IS NOT NULL")
    row = await cur.fetchone()
    if row is None or not row[0]:
        return []
    cur = await conn.execute("SELECT version, checksum FROM schema_migration")
    return [AppliedMigration(v, c) for v, c in await cur.fetchall()]


async def migrate(conn: AsyncConnection) -> list[Migration]:
    """Apply pending migrations and return them; [] when the schema is up to date.

    `conn` must be in autocommit mode (kiroku.db.connect gives one). Otherwise every
    statement would join one implicit transaction that is never committed.
    """
    if not conn.autocommit:
        raise ValueError("migrate() needs an autocommit connection; use kiroku.db.connect")
    await conn.execute("SELECT pg_advisory_lock(%s)", [ADVISORY_LOCK_KEY])
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration (
                version    text PRIMARY KEY,
                name       text NOT NULL,
                checksum   text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        todo = pending(load_migrations(), await read_applied(conn))
        for m in todo:
            async with conn.transaction():
                await conn.execute(m.sql.encode())
                await conn.execute(
                    "INSERT INTO schema_migration (version, name, checksum) VALUES (%s, %s, %s)",
                    [m.version, m.name, m.checksum],
                )
        return todo
    finally:
        # A dead connection already released its session lock; unlocking would raise
        # and hide the error that killed it.
        if not conn.broken and not conn.closed:
            await conn.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_KEY])


def new_migration(name: str, now: datetime | None = None, directory: Path = MIGRATIONS_DIR) -> Path:
    """Create an empty, correctly named migration file and return its path."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    path = directory / f"{stamp}_{name}.sql"
    if FILENAME.match(path.name) is None:
        raise MigrationError(f"migration name {name!r} must be lowercase letters, digits, and _")
    if path.exists():
        raise MigrationError(f"{path} already exists")
    path.write_text(f"-- {name}\n")
    return path
