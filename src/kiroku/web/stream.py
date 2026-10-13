"""One LISTEN connection per serve process; notifications are only wake-up hints."""

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from itertools import groupby

from fastapi import Request

from kiroku import transcript
from kiroku.db import Pool, connect
from kiroku.web.models import Utterance

log = logging.getLogger(__name__)
POLL_S = 5.0
KEEPALIVE_S = 15.0


class TranscriptHub:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.subscribers: dict[int, set[asyncio.Event]] = defaultdict(set)

    @asynccontextmanager
    async def subscribe(self, session_id: int) -> AsyncGenerator[asyncio.Event]:
        wake = asyncio.Event()
        self.subscribers[session_id].add(wake)
        try:
            yield wake
        finally:
            self.subscribers[session_id].remove(wake)
            if not self.subscribers[session_id]:
                del self.subscribers[session_id]

    async def run(self) -> None:
        while True:
            try:
                async with connect(self.database_url) as conn:
                    await conn.execute("LISTEN kiroku_transcript")
                    for wakes in self.subscribers.values():
                        for wake in wakes:
                            wake.set()
                    async for notification in conn.notifies():
                        if notification.payload.isdecimal():
                            for wake in self.subscribers.get(int(notification.payload), ()):
                                wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("transcript listener unavailable (%s); retrying", type(error).__name__)
                await asyncio.sleep(POLL_S)


async def events(
    request: Request, pool: Pool, hub: TranscriptHub, session_id: int, after_ms: int
) -> AsyncIterator[str]:
    async with hub.subscribe(session_id) as wake:
        covered = await transcript.count_through(pool, session_id, after_ms)
        keepalive = time.monotonic()
        yield ": connected\n\n"
        while not await request.is_disconnected():
            wake.clear()
            sent = 0
            while rows := await transcript.page(pool, session_id, after_ms=after_ms, limit=200):
                sent += len(rows)
                for start_ms, group in groupby(rows, key=lambda row: row.audio_start_ms):
                    # One atomic group per audio-clock ID keeps comparison transcribers intact.
                    data = (
                        "["
                        + ",".join(Utterance.model_validate(r).model_dump_json() for r in group)
                        + "]"
                    )
                    yield f"id: {start_ms}\nevent: transcript\ndata: {data}\n\n"
                    after_ms = start_ms
                if await request.is_disconnected():
                    return
            current = await transcript.count_through(pool, session_id, after_ms)
            if current != covered + sent:
                # A Backfill can insert behind after_ms; audio cursors cannot describe that.
                yield "event: reconcile\ndata: {}\n\n"
            covered = current
            if time.monotonic() - keepalive >= KEEPALIVE_S:
                yield ": keepalive\n\n"
                keepalive = time.monotonic()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=POLL_S)
