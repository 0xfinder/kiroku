"""Recorded Gate and Bot API helpers for the alerting acceptance tests."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from kiroku import rules, sessions, transcript
from kiroku.budget import Unlimited
from kiroku.config import Secret
from kiroku.db import Pool
from kiroku.gate import Gate
from kiroku.gate.jev import JevGate
from kiroku.gate.windows import WindowPolicy
from kiroku.gating import GateLoop
from kiroku.notify import AlertMessage, Delivery, Notifier, Tap
from kiroku.source import StreamMeta
from kiroku.transcriber import Result, Utterance

RECORDED: list[dict[str, Any]] = [
    json.loads(line)
    for line in Path("spikes/results/2026-10-11-gate-split.jsonl").read_text().splitlines()
]
HEADER: dict[str, Any] = RECORDED[0]["header"]
CALLS = [r for r in RECORDED[1:] if r["arm"] == "split" and r["repeat"] == 0]
CHUNKS: list[str] = [
    json.loads(line)["text"]
    for line in Path("spikes/results/2026-10-11-gorgc-blast-slam.jsonl").read_text().splitlines()
]


def canonical(body: Any) -> str:
    return json.dumps(body, sort_keys=True)


def replay_transport(path: str, *, arm: str | None = None, repeat: int = 0) -> httpx.MockTransport:
    rows: list[dict[str, Any]] = [json.loads(line) for line in Path(path).read_text().splitlines()]
    cassette = {
        canonical(r["request"]): r
        for r in rows
        if "request" in r and r.get("repeat") == repeat and (arm is None or r.get("arm") == arm)
    }

    def respond(request: httpx.Request) -> httpx.Response:
        raw = json.loads(request.content)
        row = cassette.get(canonical(raw))
        if row is None:
            pytest.fail("LLM request changed since the live recording; rerun the spike")
        return httpx.Response(row.get("status", 200), json=row["response"])

    return httpx.MockTransport(respond)


async def seed(
    pool: Pool,
    url: str,
    *,
    cooldown: int = 600,
    login: str = "gorgc",
    fresh: bool = True,
    gate: Gate | None = None,
) -> tuple[int, int, datetime]:
    now = datetime.now(UTC)
    base = now - timedelta(minutes=4 if fresh else 120)
    for r in HEADER["rules"]:
        try:
            await rules.get(pool, r["name"])
        except rules.RuleError:
            await rules.add(
                pool,
                r["name"],
                r["instructions"],
                threshold=Decimal(r["threshold"]),
                cooldown_s=cooldown,
            )
    async with sessions.claim_channels(url, "twitch", [login]) as (channel,):
        session = await sessions.open_session(
            pool, channel, StreamMeta("recorded", HEADER["stream"]["title"], "Dota 2"), base
        )
    leg = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg, base)
    for i, text in enumerate(CHUNKS):
        start, end = i * 30_000, (i + 1) * 30_000
        await transcript.commit(
            pool, session, "recorded", Result(start, end, (Utterance(start, end, text),), None)
        )
    cassette = {canonical(r["request"]): r["response"] for r in CALLS}

    def respond(req: httpx.Request) -> httpx.Response:
        raw = cassette.get(canonical(json.loads(req.content)))
        if raw is None:
            pytest.fail("Jev request changed")
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        loop = GateLoop(
            pool,
            session,
            "recorded",
            gate or JevGate(http, Secret("k")),
            Unlimited(),
            lambda: dict(HEADER["stream"]),
            policy=WindowPolicy(length_ms=30_000),
        )
        loop.finish(session_ended=True)
        await loop.run()
    return channel.id, session, now


class Sink:
    name = "telegram"

    def __init__(self, notifier: Notifier | None = None) -> None:
        self._notifier = notifier
        self.messages: list[AlertMessage] = []

    async def send(self, message: AlertMessage, *, give_up_s: float) -> Delivery:
        self.messages.append(message)
        if self._notifier is not None:
            return await self._notifier.send(message, give_up_s=give_up_s)
        return Delivery(f"42:{message.alert_id}")

    async def taps(self) -> AsyncIterator[Tap]:
        for message in self.messages:
            yield Tap(
                message.alert_id, True, None, Delivery(f"42:{message.alert_id}"), message.said_at
            )

    async def confirm(self, tap: Tap, *, recorded: bool) -> None:
        pass
