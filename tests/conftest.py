"""Throwaway Postgres for tests.

One migrated template database per session, then a `CREATE DATABASE ... TEMPLATE` clone
per test. Tests may commit, use several connections, and race, just like production.

The server comes from KIROKU_TEST_ADMIN_URL, defaulting to the compose Postgres. If it is
unreachable the run fails rather than skips, because a skipped database suite looks green.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Iterator

import psycopg
import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from kiroku.db import connect
from kiroku.db.migrate import migrate

ADMIN_URL = os.environ.get(
    "KIROKU_TEST_ADMIN_URL", "postgresql://kiroku:kiroku@127.0.0.1:5432/postgres"
)


def url_for(dbname: str) -> str:
    return make_conninfo(ADMIN_URL, dbname=dbname)


def _admin() -> psycopg.Connection:
    try:
        return psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.exit(f"test Postgres unreachable ({e}). Run `docker compose up -d --wait`.", 2)


async def _migrate(url: str) -> None:
    async with connect(url) as conn:
        await migrate(conn)


@pytest.fixture(scope="session")
def template_db() -> Iterator[str]:
    name = f"kiroku_tpl_{os.getpid()}"
    with _admin() as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        asyncio.run(_migrate(url_for(name)))
        yield name
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def database_url(template_db: str) -> Iterator[str]:
    """A migrated, empty database private to this test."""
    name = f"kiroku_t_{uuid.uuid4().hex[:12]}"
    with _admin() as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template_db)
            )
        )
        yield url_for(name)
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def blank_database_url() -> Iterator[str]:
    """An empty, unmigrated database, for migration tests."""
    name = f"kiroku_b_{uuid.uuid4().hex[:12]}"
    with _admin() as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        yield url_for(name)
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
async def conn(database_url: str) -> AsyncGenerator[AsyncConnection]:
    async with connect(database_url) as c:
        yield c
