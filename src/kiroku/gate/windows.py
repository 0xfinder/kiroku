"""Windows (pure): where each one starts and ends, and the state the Gate sees for it.

Windows tile the session audio clock: each starts where the previous one ended, so they never
overlap or leave a hole, and a session reopened after a blip simply continues the chain. A
Window closes once the transcript watermark (the highest committed span end) passes its end;
an Utterance belongs to the Window holding its start.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

type JSON = str | int | float | bool | Mapping[str, JSON] | Sequence[JSON] | None


@dataclass(frozen=True, slots=True)
class WindowPolicy:
    length_ms: int = 45_000
    # Earlier speech shown as `context.earlier`: its last context_chars, from at most
    # context_lookback_ms before the Window. The spike's merged window used 300 characters.
    context_chars: int = 300
    context_lookback_ms: int = 60_000
    # A guard against a Transcriber stuck repeating itself, not a budget: ~1.5k tokens.
    new_max_chars: int = 6_000


@dataclass(frozen=True, slots=True)
class Bounds:
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class Turn:
    start_ms: int
    text: str
    # True for Streamer turns. Until a Speaker map exists (Muse), every Utterance counts.
    # Turns that don't count (casters) are context only.
    counts: bool = True


@dataclass(frozen=True, slots=True)
class GateState:
    context: Mapping[str, JSON]  # {"stream": {...}, "earlier": "...", optional "live": {...}}
    new: str

    def as_json(self) -> dict[str, JSON]:
        return {"context": dict(self.context), "new": self.new}


def due_windows(
    next_start_ms: int, watermark_ms: int, policy: WindowPolicy, *, final: bool
) -> list[Bounds]:
    """Windows ready to close, in order: full-length ones while they fit under the watermark,
    and with `final` (the session ended) the trailing partial one up to the watermark."""
    out: list[Bounds] = []
    start = next_start_ms
    while start + policy.length_ms <= watermark_ms:
        out.append(Bounds(start, start + policy.length_ms))
        start += policy.length_ms
    if final and watermark_ms > start:
        out.append(Bounds(start, watermark_ms))
    return out


def build_state(
    bounds: Bounds, turns: Sequence[Turn], stream: Mapping[str, JSON], policy: WindowPolicy
) -> GateState:
    """`new` is what the streamer said in the Window; `context` is what helps read it.

    `new`: counting turns starting in [start, end), cut to new_max_chars (the end is kept).
    `context.earlier`: turns starting in the lookback before the Window, plus non-counting
    turns inside it, cut to the last context_chars at a word boundary; omitted when empty.
    No text appears in both.
    """
    inside = [t for t in turns if bounds.start_ms <= t.start_ms < bounds.end_ms]
    new = _join(t.text for t in inside if t.counts)
    if len(new) > policy.new_max_chars:
        new = new[-policy.new_max_chars :]
    lookback = bounds.start_ms - policy.context_lookback_ms
    earlier = _join(
        t.text
        for t in turns
        if lookback <= t.start_ms < bounds.start_ms
        or (bounds.start_ms <= t.start_ms < bounds.end_ms and not t.counts)
    )
    context: dict[str, JSON] = {"stream": dict(stream)}
    if earlier:
        context["earlier"] = _tail_words(earlier, policy.context_chars)
    return GateState(context, new)


def _join(texts: Iterable[str]) -> str:
    return " ".join(s for s in (t.strip() for t in texts) if s)


def _tail_words(text: str, chars: int) -> str:
    """The last `chars` characters, without a cut-off word at the start."""
    if len(text) <= chars:
        return text
    tail = text[-chars:]
    if text[-chars - 1] == " " or " " not in tail:
        return tail
    return tail.split(" ", 1)[1]
