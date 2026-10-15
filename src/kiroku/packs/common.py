"""Shared Pack parsing, cache admission and bounded Vocabulary formatting."""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

import httpx

DATA_TTL_S = 24 * 3600
VOCABULARY_CHARS = 350
CACHE_ERRORS = (httpx.HTTPError, TimeoutError, OSError, ValueError, KeyError, IndexError)
log = logging.getLogger(__name__)


class DataError(ValueError):
    pass


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DataError("expected an object")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise DataError("expected an array")
    return cast(list[object], value)


def integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DataError("expected an integer")
    return value


def text(value: object, *, strip: bool = False) -> str:
    # League preserves API text verbatim; Dota opts into trimmed display names.
    if not isinstance(value, str):
        raise DataError("expected a nonempty string")
    result = value.strip() if strip else value
    if not result:
        raise DataError("expected a nonempty string")
    return result


def contains_term(term: str, title: str) -> bool:
    return bool(term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", title, re.I))


def budgeted_vocabulary(prefix: str, terms: Iterable[str], title: str | None) -> str:
    out = prefix
    for term in dict.fromkeys(terms):
        if len(out) + len(term) + 2 <= VOCABULARY_CHARS:
            out += f" {term},"
    remaining = VOCABULARY_CHARS - len(out) - 1
    if remaining > 0 and title is not None:
        tail = title if len(title) <= remaining else title[: remaining + 1].rpartition(" ")[0]
        out += " " + tail
    return out.strip(" ,")


class PackData:
    def __init__(
        self,
        http: httpx.AsyncClient,
        cache_dir: Path,
        *,
        retry_s: float = 0,
        fetch_timeout_s: float = 5,
        memoize: bool = False,
    ) -> None:
        self.http = http
        self.cache_dir = cache_dir
        self.retry_s = retry_s
        self.fetch_timeout_s = fetch_timeout_s
        self.memoize = memoize
        self._locks: dict[str, asyncio.Lock] = {}
        self._retry_at: dict[str, float] = {}
        self._parsed: dict[tuple[str, object], tuple[int, object]] = {}

    async def get(self, url: str, *, website: bool = False) -> object:
        response = await self.http.get(url, timeout=10)
        response.raise_for_status()
        body: object = response.json()
        return body

    async def cached[T](
        self,
        name: str,
        url: str,
        parse: Callable[[object], T],
        *,
        website: bool = False,
        memoize: bool | None = None,
    ) -> T:
        memoize = self.memoize if memoize is None else memoize
        async with self._locks.setdefault(name, asyncio.Lock()):
            path = self.cache_dir / f"{name}.json"
            stale: tuple[T] | None = None
            memo_key = (name, parse)
            try:
                stat = path.stat()
                memo = self._parsed.get(memo_key) if memoize else None
                if memo is not None and memo[0] == stat.st_mtime_ns:
                    parsed = cast(T, memo[1])
                else:
                    parsed = parse(json.loads(path.read_text()))
                    if memoize:
                        self._parsed[memo_key] = (stat.st_mtime_ns, parsed)
                stale = (parsed,)
                if time.time() - stat.st_mtime < DATA_TTL_S:
                    return parsed
            except (OSError, ValueError, KeyError, IndexError):
                pass
            if time.monotonic() < self._retry_at.get(name, 0):
                if stale is not None:
                    return stale[0]
                raise DataError(f"{name} refresh deferred after failure")
            try:
                async with asyncio.timeout(self.fetch_timeout_s):
                    body = await self.get(url, website=website)
                parsed = parse(body)
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(body))
                temporary.replace(path)
                if memoize:
                    self._parsed[memo_key] = (path.stat().st_mtime_ns, parsed)
                self._retry_at.pop(name, None)
            except CACHE_ERRORS as e:
                self._retry_at[name] = time.monotonic() + self.retry_s
                if stale is None:
                    raise
                log.warning(
                    "%s %s refresh failed; using stale cache (%s)", self.cache_dir.name, name, e
                )
                return stale[0]
            return parsed
