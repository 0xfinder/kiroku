"""Offline Bot API with recorded shapes and real getUpdates acknowledgement semantics."""

import json
from pathlib import Path
from typing import Any

import httpx

SYNTHESIZED: dict[str, Any] = json.loads(
    Path("tests/fixtures/telegram/synthesized.json").read_text()
)
SMOKE: dict[str, Any] = json.loads(Path("tests/fixtures/telegram/smoke.json").read_text())


class BotAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body: dict[str, Any] = json.loads(request.content)
        self.calls.append((method, body))
        if method == "sendMessage":
            if body["chat_id"] != 42 or body["parse_mode"] != "HTML":
                return httpx.Response(400, json={"ok": False, "description": "bad request"})
            self.sent.append(body)
            real = next(c["response"] for c in SMOKE["calls"] if c["method"] == "sendMessage")
            copied: dict[str, Any] = json.loads(json.dumps(real))
            copied["result"]["chat"]["id"] = 42
            copied["result"]["message_id"] = int(
                body["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
            )
            copied["result"]["text"] = body["text"]
            return httpx.Response(200, json=copied)
        if method == "getUpdates":
            offset = body.get("offset", 0)
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        return httpx.Response(200, json={"ok": True, "result": True})

    def tap(self, alert_id: int, *, applies: bool = True, chat_id: int = 42) -> None:
        updates = [
            u
            for c in SMOKE["calls"] + SYNTHESIZED["calls"]
            if c["method"] == "getUpdates"
            for u in c["response"].get("result", [])
            if "callback_query" in u
        ]
        if updates:
            update: dict[str, Any] = json.loads(json.dumps(updates[0]))
        else:
            # Bot API documented shape; the README distinguishes synthesized callbacks.
            update = {
                "update_id": 1,
                "callback_query": {
                    "id": "callback",
                    "message": {"message_id": 1, "date": 1, "chat": {"id": 42, "type": "private"}},
                    "data": "l:1:y",
                },
            }
        update["update_id"] = max((u["update_id"] for u in self.updates), default=0) + 1
        query = update["callback_query"]
        query["data"] = f"l:{alert_id}:{'y' if applies else 'n'}"
        query["message"]["chat"]["id"] = chat_id
        query["message"]["message_id"] = alert_id
        self.updates.append(update)
