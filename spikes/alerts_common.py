"""Helpers for isolated, sanitized issue #5 live measurements. Never migrates kiroku."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from kiroku import gating, rules, sessions, transcript
from kiroku.db import connect
from kiroku.db.migrate import migrate
from kiroku.gate import GateAnswer, Mode, rule_question
from kiroku.gate.windows import Bounds, GateState
from kiroku.source import StreamMeta
from kiroku.transcriber import Result, Utterance

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "spikes/results/2026-10-11-gate-split.jsonl"
TRANSCRIBER = "groq:whisper-large-v3-turbo"
ADMIN = "postgresql://kiroku:kiroku@127.0.0.1:5432/postgres"


async def scratch(name="kiroku_spike_5"):
    url = make_conninfo(ADMIN, dbname=name)
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    async with connect(url) as conn:
        await migrate(conn)
    return url


async def seed(pool, url, *, cooldown=600, login="gorgc", fresh=True, now=None, source=SOURCE):
    recorded = [json.loads(line) for line in source.read_text().splitlines()]
    header = recorded[0]["header"]
    split = [r for r in recorded[1:] if r["arm"] == "split" and r["repeat"] == 0]
    # A new session per arm keeps both Novelty histories and ledger rows isolated.
    now = now or datetime.now(UTC)
    base = now - timedelta(minutes=4 if fresh else 120)
    for r in header["rules"]:
        try:
            existing = await rules.get(pool, r["name"])
        except rules.RuleError:
            existing = None
        if existing is None:
            await rules.add(
                pool,
                r["name"],
                r["instructions"],
                threshold=Decimal(r["threshold"]),
                cooldown_s=cooldown,
            )
        else:
            await rules.edit(pool, existing.name, cooldown_s=cooldown)
    async with sessions.claim_channels(url, "twitch", [login]) as (channel,):
        session = await sessions.open_session(
            pool,
            channel,
            StreamMeta(str(time.time_ns()), header["stream"]["title"], "Dota 2"),
            base,
        )
    leg = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg, base)
    active = await rules.active(pool, None)
    for row in split:
        i = row["chunk"]
        start, end = i * 30_000, (i + 1) * 30_000
        state = GateState(row["request"]["state"]["context"], row["request"]["state"]["new"])
        await transcript.commit(
            pool,
            session,
            TRANSCRIBER,
            Result(start, end, (Utterance(start, end, state.new),), None),
        )
        answer = GateAnswer(
            "jev:jev-1.13.0",
            {r.id: row["scores"][r.name] for r in active},
            Mode.WATCHING_PRO,
            1.0,
            (),
        )
        await gating.commit(
            pool,
            session,
            TRANSCRIBER,
            Bounds(start, end),
            state,
            [rule_question(r) for r in active],
            gating.Answered(answer),
        )
    return channel, session, now


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self, *, pace_s=0, token=None, chat_id=None):
        self.inner = httpx.AsyncHTTPTransport()
        self.rows = []
        self.pace_s, self.last = pace_s, 0.0
        self.token, self.chat_id = token, chat_id

    def sanitize(self, raw):
        # Personal names/user IDs are removed along with token and destination.
        if isinstance(raw, dict):
            return {
                k: ("<CHAT_ID>" if k == "id" and v == self.chat_id else self.sanitize(v))
                for k, v in raw.items()
                if k not in ("first_name", "last_name", "username")
            }
        if isinstance(raw, list):
            return [self.sanitize(v) for v in raw]
        if isinstance(raw, str):
            if self.token:
                raw = raw.replace(self.token, "<BOT_TOKEN>")
            if self.chat_id:
                raw = raw.replace(str(self.chat_id), "<CHAT_ID>")
        if isinstance(raw, int) and raw == self.chat_id:
            return "<CHAT_ID>"
        return raw

    async def handle_async_request(self, request):
        await asyncio.sleep(max(0, self.last + self.pace_s - time.monotonic()))
        self.last = time.monotonic()
        response = await self.inner.handle_async_request(request)
        content = await response.aread()
        try:
            body = json.loads(content)
        except ValueError:
            body = {"malformed": True}
        self.rows.append(
            self.sanitize(
                {
                    "method": request.url.path.rsplit("/", 1)[-1],
                    "request": json.loads(request.content),
                    "response": body,
                    "status": response.status_code,
                    "headers": {
                        k: v
                        for k, v in response.headers.items()
                        if k.startswith("x-ratelimit") or k == "retry-after"
                    },
                }
            )
        )
        return response

    async def aclose(self):
        await self.inner.aclose()
