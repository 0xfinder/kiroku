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
