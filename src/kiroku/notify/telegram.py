"""Telegram Bot API. The token is in the URL: never log an httpx exception or request."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Any, Final

import httpx

from kiroku.config import Secret
from kiroku.gate.windows import JSON
from kiroku.llm import object_body
from kiroku.notify import (
    AlertMessage,
    Delivery,
    NotifierRejected,
    NotifierUnavailable,
    NotifierUnsure,
    Tap,
)

log = logging.getLogger(__name__)
CALLBACK: Final = re.compile(r"l:(\d{1,19}):([yn])")


@dataclass(frozen=True, slots=True)
class Callback:
    query_id: str
    message_id: int


def keyboard(alert_id: int, chosen: bool | None = None) -> dict[str, JSON]:
    return {
        "inline_keyboard": [
            [
                {"text": "👍 ✓" if chosen is True else "👍", "callback_data": f"l:{alert_id}:y"},
                {"text": "👎 ✓" if chosen is False else "👎", "callback_data": f"l:{alert_id}:n"},
            ]
        ]
    }


def render_html(message: AlertMessage) -> str:
    text = (
        f"🔔 <b>{escape(message.channel)}</b> · {escape(message.rule)}\n{escape(message.summary)}"
    )
    if message.quote:
        text += f"\n<i>“{escape(message.quote)}”</i>"
    text += f"\n{message.said_at.astimezone(UTC):%H:%M} UTC · Gate {message.probability:.2f}"
    if not message.gate_model.startswith("jev:"):
        text += f" · ⚠ Gate Fallback ({escape(message.gate_model)})"
    if message.degraded:
        text += f" · ⚠ no summary: {escape(message.degraded)}"
    return text


def integer(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError("expected an integer")
    return raw


def parse_tap(raw: Mapping[str, Any], chat_id: int) -> Tap | None:
    callback = object_body(raw["callback_query"])
    message = object_body(callback["message"])
    chat = object_body(message["chat"])
    data = callback["data"]
    if integer(chat["id"]) != chat_id or not isinstance(data, str):
        return None
    match = CALLBACK.fullmatch(data)
    if match is None:
        return None
    alert_id = int(match[1])
    if not 0 < alert_id <= 2**63 - 1:
        return None
    query_id = callback["id"]
    if not isinstance(query_id, str):
        raise TypeError("expected callback id")
    message_id = integer(message["message_id"])
    return Tap(
        alert_id,
        match[2] == "y",
        Callback(query_id, message_id),
        Delivery(f"{chat_id}:{message_id}"),
        datetime.fromtimestamp(integer(message["date"]), UTC),
    )


class TelegramNotifier:
    name = "telegram"

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: Secret,
        chat_id: int,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_timeout_s: int = 50,
    ) -> None:
        self._http, self._token, self._chat = http, token, chat_id
        self._sleep, self._clock, self._poll_timeout = sleep, clock, poll_timeout_s
        self._not_before = 0.0
        self._sending = asyncio.Lock()

    def _clean(self, text: str) -> str:
        return text.replace(self._token.reveal(), "<BOT_TOKEN>").replace(
            str(self._chat), "<CHAT_ID>"
        )

    async def _post(
        self, method: str, body: Mapping[str, JSON], request_timeout_s: float
    ) -> httpx.Response:
        return await self._http.post(
            f"https://api.telegram.org/bot{self._token.reveal()}/{method}",
            json=body,
            timeout=request_timeout_s,
        )

    async def send(self, message: AlertMessage, *, give_up_s: float) -> Delivery:
        async with self._sending:
            return await self._send(message, give_up_s)

    async def _send(self, message: AlertMessage, give_up_s: float) -> Delivery:
        first, attempt, wait = self._clock(), 0, 0.0
        body: dict[str, JSON] = {
            "chat_id": self._chat,
            "text": render_html(message),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": keyboard(message.alert_id),
        }
        while True:
            wait = max(wait, self._not_before - self._clock(), 0.0)
            left = give_up_s - (self._clock() - first)
            if left <= 0 or wait >= left:
                raise NotifierUnavailable("Telegram retry deadline reached")
            if wait:
                await self._sleep(wait)
            attempt += 1
            self._not_before = max(self._not_before, self._clock() + 1.0)
            try:
                response = await self._post("sendMessage", body, min(15.0, left - wait))
            except (httpx.ConnectError, httpx.ConnectTimeout):
                wait = float(min(2 ** (attempt - 1), 30))
                continue
            except httpx.RequestError as e:
                raise NotifierUnsure(f"Telegram {type(e).__name__}") from None
            if response.status_code >= 500:
                raise NotifierUnsure(f"Telegram HTTP {response.status_code}")
            try:
                raw = object_body(response.json())
                if response.status_code == 429:
                    retry = object_body(raw["parameters"])["retry_after"]
                    if raw.get("ok") is not False or integer(retry) <= 0:
                        raise ValueError("invalid retry_after")
                    wait = float(retry)
                    self._not_before = max(self._not_before, self._clock() + wait)
                    continue
                if response.status_code >= 400 or raw.get("ok") is False:
                    description = raw.get("description")
                    reason = (
                        description if isinstance(description, str) else "Bot API rejected send"
                    )
                    raise NotifierRejected(self._clean(reason))
                if raw.get("ok") is not True:
                    raise ValueError("missing ok")
                result = object_body(raw["result"])
                if integer(object_body(result["chat"])["id"]) != self._chat:
                    raise ValueError("unexpected chat")
                return Delivery(f"{self._chat}:{integer(result['message_id'])}")
            except (ValueError, KeyError, TypeError):
                raise NotifierUnsure("Telegram malformed send response") from None

    async def taps(self) -> AsyncGenerator[Tap]:
        offset: int | None = None
        while True:
            body: dict[str, JSON] = {
                "timeout": self._poll_timeout,
                "allowed_updates": ["callback_query"],
            }
            if offset is not None:
                body["offset"] = offset
            try:
                response = await self._post("getUpdates", body, self._poll_timeout + 10.0)
                if response.status_code == 409:
                    log.error("Telegram getUpdates conflict: another poller is active")
                    await self._sleep(60.0)
                    continue
                if response.status_code >= 400:
                    log.warning("Telegram getUpdates HTTP %d", response.status_code)
                    await self._sleep(5.0)
                    continue
                raw = object_body(response.json())
                if raw.get("ok") is not True or not isinstance(raw.get("result"), list):
                    raise ValueError("invalid updates")
                updates: list[Any] = raw["result"]
                for update in updates:
                    item = object_body(update)
                    update_id = integer(item["update_id"])
                    try:
                        tap = parse_tap(item, self._chat)
                    except (ValueError, KeyError, TypeError, OverflowError):
                        tap = None
                    if tap is not None:
                        yield tap  # consumer commits before resuming and acknowledging the batch
                    else:
                        callback = object_body(item.get("callback_query", {}))
                        query = callback.get("id")
                        if isinstance(query, str):
                            await self._best_effort(
                                "answerCallbackQuery",
                                {
                                    "callback_query_id": query,
                                    "text": "Not for this bot",
                                },
                            )
                    offset = update_id + 1
            except httpx.RequestError as e:
                log.warning("Telegram poll failed (%s)", type(e).__name__)
                await self._sleep(5.0)
            except (ValueError, KeyError, TypeError):
                log.warning("Telegram malformed updates")
                await self._sleep(5.0)

    async def _best_effort(self, method: str, body: Mapping[str, JSON]) -> None:
        try:
            response = await self._post(method, body, 10.0)
            if response.status_code >= 400:
                log.debug("Telegram %s HTTP %d", method, response.status_code)
        except httpx.RequestError as e:
            log.warning("Telegram %s failed (%s)", method, type(e).__name__)

    async def confirm(self, tap: Tap, *, recorded: bool) -> None:
        if not isinstance(tap.handle, Callback):
            return
        await self._best_effort(
            "answerCallbackQuery",
            {
                "callback_query_id": tap.handle.query_id,
                "text": ("Saved 👍" if tap.applies else "Saved 👎")
                if recorded
                else "Unknown alert",
            },
        )
        if recorded:
            await self._best_effort(
                "editMessageReplyMarkup",
                {
                    "chat_id": self._chat,
                    "message_id": tap.handle.message_id,
                    "reply_markup": keyboard(tap.alert_id, tap.applies),
                },
            )
