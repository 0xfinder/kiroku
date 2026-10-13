"""Shared production seeding for offline and live Notes acceptance."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from kiroku import gating, segments, sessions, transcript
from kiroku.db import Pool
from kiroku.gate import GateAnswer, Mode
from kiroku.gate.windows import Bounds, GateState
from kiroku.source import StreamMeta
from kiroku.transcriber import Result, Utterance

TRANSCRIBER = "groq:whisper-large-v3-turbo"


def read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def seed(
    pool: Pool,
    url: str,
    fixture: Literal["league", "general"] = "general",
    *,
    closed: bool = True,
    speech: bool = True,
    gate: bool = True,
    age: timedelta = timedelta(),
) -> tuple[int, int, datetime]:
    now = datetime.now(UTC)
    if fixture == "league":
        path = Path("spikes/results/2026-10-12-league-vocabulary/pack.jsonl")
        rows: list[dict[str, Any]] = await asyncio.to_thread(read, path)
        title = "Worlds 2026 RED Kalunga vs FlyQuest, game 1"
        category, pack = "League of Legends", "league"
        # The real completed-match snapshot from #6 supplies spelling and state only.
        recorded = await asyncio.to_thread(
            read, Path("spikes/results/2026-10-12-league-gate.jsonl")
        )
        live = next(
            r["request"]["state"]["context"]["live"]
            for r in recorded
            if "live" in r.get("request", {}).get("state", {}).get("context", {})
        )
    else:
        rows = await asyncio.to_thread(
            read, Path("spikes/results/2026-10-11-gorgc-blast-slam.jsonl")
        )
        rows = [
            {
                "start_ms": r["chunk"] * 30_000,
                "end_ms": (r["chunk"] + 1) * 30_000,
                "text": r["text"],
            }
            for r in rows
        ]
        title, category, pack, live = "Gorgc BLAST Slam", "Dota 2", None, None
    end = rows[-1]["end_ms"]
    base = now - timedelta(milliseconds=end) - age
    async with sessions.claim_channels(url, "twitch", [fixture]) as (channel,):
        session = await sessions.open_session(
            pool,
            channel,
            StreamMeta(str(time.time_ns()), title, category),
            base,
        )
    leg = await sessions.next_leg(pool, channel, session)
    await sessions.record_leg(pool, leg, base)
    await segments.record(pool, session, 0, StreamMeta("fixture", title, category), pack)
    for row in rows:
        start, stop = row["start_ms"], row["end_ms"]
        await transcript.commit(
            pool,
            session,
            TRANSCRIBER,
            Result(
                start,
                stop,
                (Utterance(start, stop, row["text"]),) if speech else (),
                None,
            ),
        )
    if gate:
        start = 0
        while start < end:
            stop = min(start + 45_000, end)
            state = GateState(
                {
                    "stream": {"title": title, "category": category},
                    **({"live": live} if live else {}),
                },
                "fixture",
            )
            await gating.commit(
                pool,
                session,
                TRANSCRIBER,
                Bounds(start, stop),
                state,
                (),
                gating.Answered(
                    GateAnswer(
                        "jev:jev-1.13.0",
                        {},
                        Mode.WATCHING_PRO,
                        1.0,
                        (),
                    )
                ),
                game_key="league:117133830114188936:1" if live else None,
            )
            start = stop
    await sessions.end_leg(pool, leg, end)
    if closed:
        await sessions.close_open_sessions(pool, channel, now - age)
    return channel.id, session, now
