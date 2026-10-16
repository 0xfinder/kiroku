"""The Transcriber interface. One method fits request/response (Groq) and streaming (Muse).

A Transcriber consumes the session's audio events in order and yields Results. Each Result
finalizes one span of the audio clock: the utterances inside it (possibly none, for silence)
and the paid call that produced them, if any. Spans are contiguous and non-overlapping within
a leg. The Transcriber never touches the database; the caller commits each Result.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol

from kiroku.audio import AudioEvent
from kiroku.budget import BudgetGuard
from kiroku.errors import KirokuError
from kiroku.ledger import CostEntry


class TranscriberAuthError(KirokuError):
    """401/403 from a transcription provider: the key is wrong.

    The immutable `costs` are dispatches that were sent and lost before the refusal; they
    may bill anyway, so the consumer records them exactly once before it stops. Speech
    that was not transcribed stays recoverable.
    """

    def __init__(self, reason: str, costs: tuple[CostEntry, ...]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.costs = costs


@dataclass(frozen=True, slots=True)
class Utterance:
    start_ms: int  # session audio clock
    end_ms: int
    text: str
    speaker_label: str | None = None


@dataclass(frozen=True, slots=True)
class Result:
    start_ms: int
    end_ms: int
    utterances: tuple[Utterance, ...]
    cost: CostEntry | None  # session_id is filled in by the caller


@dataclass(frozen=True, slots=True)
class Context:
    vocabulary: Callable[[], str]  # read per call: the title and Pack can change
    guard: BudgetGuard  # asked before every paid call; a refused chunk is left as a gap
    previous_text: str = ""  # tail of the stored transcript, to seed the first prompt
    # Archived audio (Backfill), not live: it can wait. It must never delay live audio that
    # shares the Transcriber's limits, and it is read only as fast as it is transcribed.
    background: bool = False


class Transcriber(Protocol):
    @property
    def name(self) -> str: ...  # stored in utterance.transcriber, e.g. "groq:whisper-..."

    def transcribe(
        self, audio: AsyncIterator[AudioEvent], context: Context
    ) -> AsyncIterator[Result]:
        """Yield Results until `audio` ends. Retries and rate limits are handled inside."""
        ...
