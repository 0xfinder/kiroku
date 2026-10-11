"""Postgres access: psycopg 3, async, raw SQL.

Each table has one owning module that writes it (kiroku.ledger owns cost_ledger). There is
no ORM and no generic repository layer.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict

from kiroku.errors import KirokuError


class DatabaseUnavailable(KirokuError):
    pass


@asynccontextmanager
async def connect(database_url: str) -> AsyncGenerator[AsyncConnection]:
    """One autocommit connection. Callers open transactions explicitly."""
    try:
        conn = await AsyncConnection.connect(database_url, autocommit=True)
    except psycopg.OperationalError as e:
        params = conninfo_to_dict(database_url)
        where = f"{params.get('host', 'localhost')}:{params.get('port', 5432)}"
        raise DatabaseUnavailable(
            f"cannot reach Postgres at {where}. Is it running? Try `docker compose up -d --wait`."
        ) from e
    async with conn:
        yield conn
