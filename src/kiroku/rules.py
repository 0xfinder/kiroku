"""Rules: the only writer of `rule`.

A Rule is the user's yes/no condition plus its threshold. The Gate wraps the condition in
its own framing (kiroku.gate.rule_question), so a Rule never names state fields. Rules are
disabled, never deleted. #4 adds edit/enable/disable and the CLI over this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from kiroku.db import Pool


@dataclass(frozen=True, slots=True)
class Rule:
    id: int
    name: str
    instructions: str
    threshold: Decimal
    pack: str | None
    cooldown_s: int


_COLUMNS: Final = "id, name, instructions, threshold, pack, cooldown_s"


async def add(
    pool: Pool,
    name: str,
    instructions: str,
    *,
    threshold: Decimal,
    cooldown_s: int = 600,
    pack: str | None = None,
    owner: str = "me",
) -> Rule:
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            INSERT INTO rule (owner, name, instructions, threshold, cooldown_s, pack)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            [owner, name, instructions.strip(), threshold, cooldown_s, pack],
        )
        row = await cur.fetchone()
    assert row is not None
    return Rule(*row)


async def active(pool: Pool, pack: str | None) -> list[Rule]:
    """Enabled Rules that apply to `pack` (unscoped Rules apply everywhere), by id."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            SELECT {_COLUMNS} FROM rule
            WHERE enabled AND (pack IS NULL OR pack = %s)
            ORDER BY id
            """,
            [pack],
        )
        rows = await cur.fetchall()
    return [Rule(*row) for row in rows]
