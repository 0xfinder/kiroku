"""Postgres access: psycopg 3, async, raw SQL.

Each table has one owning module that writes it (kiroku.ledger owns cost_ledger). There is
no ORM and no generic repository layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool

from kiroku.errors import KirokuError

# Small on purpose: a 2 vCPU host, an I/O-bound app, and Postgres max_connections=30.
POOL_MAX_SIZE = 5

type Pool = AsyncConnectionPool[AsyncConnection]

log = logging.getLogger(__name__)


async def retrying[T](what: str, fn: Callable[[], Awaitable[T]]) -> T:
    """Retry a database operation, never the paid call that preceded it."""
    delay = 1.0
    while True:
        try:
            return await fn()
        except psycopg.OperationalError:
            log.warning("%s failed; retrying", what)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


class DatabaseUnavailable(KirokuError):
    pass


class SchemaOutOfDate(KirokuError):
    pass


@asynccontextmanager
async def connect(database_url: str) -> AsyncGenerator[AsyncConnection]:
    """One autocommit connection. Callers open transactions explicitly."""
    try:
        conn = await AsyncConnection.connect(database_url, autocommit=True)
    except psycopg.OperationalError as e:
        params = conninfo_to_dict(database_url)
        where = f"{params.get('host', 'localhost')}:{params.get('port', 5432)}"
        cause = str(e).strip().splitlines()[-1] if str(e).strip() else type(e).__name__
        raise DatabaseUnavailable(
            f"cannot connect to Postgres at {where}: {cause}. "
            "If it is not running, try `docker compose up -d --wait`."
        ) from e
    async with conn:
        yield conn


@asynccontextmanager
async def open_pool(database_url: str) -> AsyncGenerator[Pool]:
    """The pool a long-running process shares across tasks. Connections are autocommit.

    Refuses to start while migrations are pending, so the schema has one trigger
    (`kiroku db migrate`) and the app never runs against a schema it doesn't expect.
    """
    from kiroku.db.migrate import load_migrations, pending, read_applied

    async with connect(database_url) as conn:
        if pending(load_migrations(), await read_applied(conn)):
            raise SchemaOutOfDate("the database schema is behind; run `kiroku db migrate`")
    pool: Pool = AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=POOL_MAX_SIZE,
        kwargs={"autocommit": True},
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield pool
    finally:
        await pool.close()
