from pathlib import Path

import pytest

from kiroku.source import NotLive, Online, Probe, ProbeFailed, StreamMeta
from kiroku.watch import ChannelState, Close, Live, Offline, Open, Unknown, Update, step

A = StreamMeta("a", "title", "Dota 2")
A_RETITLED = StreamMeta("a", "new title", "Dota 2")
B = StreamMeta("b", "title", "Dota 2")


@pytest.mark.parametrize(
    ("state", "probe", "expected"),
    [
        (Live(A), ProbeFailed("x"), (Live(A), ())),
        (Unknown(), NotLive(), (Offline(), (Close(),))),
        (Offline(), NotLive(), (Offline(), ())),
        (Live(A), NotLive(), (Live(A, 1), ())),
        (Live(A, 1), NotLive(), (Offline(), (Close(),))),
        (Unknown(), Online(A), (Live(A), (Open(A),))),
        (Offline(), Online(A), (Live(A), (Open(A),))),
        (Live(A, 1), Online(A), (Live(A), ())),
        (Live(A), Online(A_RETITLED), (Live(A_RETITLED), (Update(A_RETITLED),))),
        (Live(A), Online(B), (Live(B), (Close(), Open(B)))),
    ],
)
def test_lifecycle(state: ChannelState, probe: Probe, expected: object) -> None:
    assert step(state, probe) == expected


@pytest.mark.parametrize("phase", ["channel", "alerts", "timeout"])
async def test_watch_shutdown_is_ordered_bounded_and_interruptible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    import asyncio
    from typing import Any

    import kiroku.watch as module

    started, stopping, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events: list[str] = []

    class Worker:
        def wake(self) -> None:
            pass

        def finish(self) -> None:
            events.append("finish alerts")
            draining.set()

        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                events.append("cancel alerts")

    async def channel(*args: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopping.set()
            if phase == "channel":
                await asyncio.Event().wait()
            events.append("channels stopped")

    async def taps(*args: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancel taps")

    monkeypatch.setattr(module, "_watch_channel", channel)
    monkeypatch.setattr(module.labels, "run_taps", taps)
    monkeypatch.setattr(module, "ALERT_DRAIN_TIMEOUT_S", 0.01 if phase == "timeout" else 20)
    watch_shell: Any = module.watch
    task = asyncio.create_task(
        watch_shell(
            None,
            [(None, None)],
            None,
            tmp_path,
            budget=None,
            gate=None,
            alerts=Worker(),
            notifier=object(),
            database_url="unused",
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), 1)
    if phase == "channel":
        task.cancel()
    else:
        await asyncio.wait_for(draining.wait(), 1)
        assert events[:2] == ["channels stopped", "finish alerts"]
        if phase == "alerts":
            task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert "cancel alerts" in events and "cancel taps" in events
    if phase == "channel":
        assert "finish alerts" not in events
