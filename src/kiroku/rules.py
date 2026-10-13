"""Rules: the only writer of `rule`.

A Rule is the user's yes/no condition plus its threshold. The Gate wraps the condition in
its own framing (kiroku.gate.rule_question), so a Rule never names state fields. Rules are
disabled, never deleted: gate_decision rows reference them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import psycopg

from kiroku.db import Pool
from kiroku.errors import KirokuError

NAME_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,62}")
PACKS: Final = ("league", "dota")  # D11/D12; Category selects one


class RuleError(KirokuError):
    pass


@dataclass(frozen=True, slots=True)
class Rule:
    id: int
    name: str
    instructions: str
    threshold: Decimal
    pack: str | None
    cooldown_s: int
    enabled: bool = True


_COLUMNS: Final = "id, name, instructions, threshold, pack, cooldown_s, enabled"


def check(
    name: str,
    instructions: str,
    threshold: Decimal,
    *,
    cooldown_s: int = 0,
    pack: str | None = None,
) -> None:
    """The constraints the table enforces, as messages a user can act on."""
    if not NAME_PATTERN.fullmatch(name):
        raise RuleError(f"rule name {name!r}: use lowercase letters, digits and _, e.g. draft_take")
    if not 1 <= len(instructions.strip()) <= 2000:
        raise RuleError("instructions must be 1 to 2000 characters")
    if "`" in instructions:
        raise RuleError("instructions can't contain backticks; the Gate names the state fields")
    if not (threshold.is_finite() and Decimal(0) < threshold <= Decimal(1)):
        raise RuleError(f"threshold {threshold} must be above 0 and at most 1")
    if threshold != threshold.quantize(Decimal("0.01")):
        raise RuleError(f"threshold {threshold}: use at most two decimals, e.g. 0.65")
    if cooldown_s < 0:
        raise RuleError("cooldown can't be negative")
    if pack is not None and pack not in PACKS:
        raise RuleError(f"pack {pack!r}: use one of {', '.join(PACKS)}")


async def add(
    pool: Pool,
    name: str,
    instructions: str,
    *,
    threshold: Decimal,
    cooldown_s: int = 600,
    pack: str | None = None,
    owner: str = "me",
    enabled: bool = True,
) -> Rule:
    check(name, instructions, threshold, cooldown_s=cooldown_s, pack=pack)
    async with pool.connection() as conn:
        try:
            cur = await conn.execute(
                f"""
                INSERT INTO rule (owner, name, instructions, threshold, cooldown_s, pack, enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                [owner, name, instructions.strip(), threshold, cooldown_s, pack, enabled],
            )
        except psycopg.errors.UniqueViolation:
            raise RuleError(f"a rule named {name} already exists; use `kiroku rule edit`") from None
        row = await cur.fetchone()
    assert row is not None
    return Rule(*row)


async def get(pool: Pool, name: str, *, owner: str = "me") -> Rule:
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM rule WHERE owner = %s AND name = %s", [owner, name]
        )
        row = await cur.fetchone()
    if row is None:
        raise RuleError(f"no rule named {name}; see `kiroku rule list`")
    return Rule(*row)


async def edit(
    pool: Pool,
    name: str,
    *,
    instructions: str | None = None,
    threshold: Decimal | None = None,
    cooldown_s: int | None = None,
    pack: str | None = None,
    clear_pack: bool = False,
    enabled: bool | None = None,
    owner: str = "me",
) -> Rule:
    """Change the given fields. Past decisions keep the wording they were asked with."""
    old = await get(pool, name, owner=owner)
    new_instructions = instructions.strip() if instructions is not None else old.instructions
    new_threshold = threshold if threshold is not None else old.threshold
    new_cooldown = cooldown_s if cooldown_s is not None else old.cooldown_s
    new_pack = None if clear_pack else (pack if pack is not None else old.pack)
    check(name, new_instructions, new_threshold, cooldown_s=new_cooldown, pack=new_pack)
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            UPDATE rule SET instructions = %s, threshold = %s, cooldown_s = %s, pack = %s,
                            enabled = coalesce(%s, enabled), updated_at = now()
            WHERE id = %s
            RETURNING {_COLUMNS}
            """,
            [
                new_instructions,
                new_threshold,
                new_cooldown,
                new_pack,
                enabled,
                old.id,
            ],
        )
        row = await cur.fetchone()
    assert row is not None
    return Rule(*row)


async def set_enabled(pool: Pool, name: str, enabled: bool, *, owner: str = "me") -> Rule:
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"""
            UPDATE rule SET enabled = %s, updated_at = now()
            WHERE owner = %s AND name = %s
            RETURNING {_COLUMNS}
            """,
            [enabled, owner, name],
        )
        row = await cur.fetchone()
    if row is None:
        raise RuleError(f"no rule named {name}; see `kiroku rule list`")
    return Rule(*row)


async def all_rules(pool: Pool, *, owner: str = "me") -> list[Rule]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM rule WHERE owner = %s ORDER BY name", [owner]
        )
        rows = await cur.fetchall()
    return [Rule(*row) for row in rows]


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
