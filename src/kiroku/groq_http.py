"""Retry timing shared by Groq audio and JSON completions."""

import re
from collections.abc import Mapping

_GO_DURATION = re.compile(
    r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?"
)


def go_seconds(text: str) -> float | None:
    """'2m59.56s' -> 179.56, '750ms' -> 0.75. Groq's reset headers use Go durations."""
    match = _GO_DURATION.fullmatch(text.strip())
    if not match or not any(match.groups()):
        return None
    h, m, s, milli = (float(g) if g else 0.0 for g in match.groups())
    return h * 3600 + m * 60 + s + milli / 1000


def retry_delay(status: int | None, headers: Mapping[str, str], attempt: int) -> float:
    """Seconds to wait before retrying the same chunk.

    429: `retry-after` if present, else the larger of the audio-seconds and request reset
    headers. Otherwise (5xx, network): 1, 2, 4 ... seconds, capped at 60.
    """
    backoff = float(min(2 ** (attempt - 1), 60))
    if status != 429:
        return backoff
    if (value := headers.get("retry-after")) is not None:
        try:
            return max(float(value), 1.0)
        except ValueError:
            pass
    resets = [
        go_seconds(headers[h])
        for h in (
            "x-ratelimit-reset-audio-seconds",
            "x-ratelimit-reset-tokens",
            "x-ratelimit-reset-requests",
        )
        if h in headers
    ]
    waits = [r for r in resets if r is not None]
    return max(waits) if waits else backoff
