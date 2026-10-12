"""Send exactly one test Alert, wait at most 20 minutes, and label through production code."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from alerts_common import Recorder, scratch, seed

from kiroku import alerting, labels
from kiroku.config import SecretName, Settings
from kiroku.db import open_pool
from kiroku.notify import AlertMessage
from kiroku.notify.telegram import TelegramNotifier


class TestTelegram(TelegramNotifier):
    async def _post(self, method, body, request_timeout_s):
        if method == "sendMessage":
            body = {**body, "text": "kiroku test:\n" + body["text"]}
        return await super()._post(method, body, request_timeout_s)


async def main(out):
    settings = Settings.load()
    token = settings.secrets.require(SecretName.TELEGRAM)
    chat = int(settings.secrets.require(SecretName.TELEGRAM_CHAT).reveal())
    url = await scratch()
    recorder = Recorder(token=token.reveal(), chat_id=chat)
    out.mkdir(parents=True, exist_ok=True)

    def save():
        (out / "smoke.json").write_text(
            json.dumps({"origin": "live Bot API", "calls": recorder.rows}, indent=2)
        )

    async with open_pool(url) as pool, httpx.AsyncClient(transport=recorder) as http:
        channel, session, now = await seed(pool, url)
        candidates = await alerting.pending(pool, [channel.id])
        candidate = next(
            c for c in candidates if c.session_id == session and c.rule == "predicts_winner"
        )
        content = {
            "claim": "Spirit will beat PARIVISION",
            "summary": "Gorgc rates Spirit about 70-30 to beat PARIVISION in this game.",
            "quote": None,
        }
        alert_id = await alerting.commit(
            pool, candidate, alerting.Sending("smoke", content, None, None, "telegram", ())
        )
        notifier = TestTelegram(http, token, chat)
        delivery = await notifier.send(
            AlertMessage(
                alert_id,
                "gorgc",
                "predicts_winner",
                content["summary"],
                None,
                now,
                Decimal(".68"),
                "jev:jev-1.13.0",
            ),
            give_up_s=30,
        )
        await alerting.mark_sent(pool, alert_id, delivery, datetime.now(UTC))
        save()
        print("Sent one kiroku test alert; waiting up to 20 minutes for a tap.", flush=True)
        taps = notifier.taps()
        try:
            async with asyncio.timeout(1200):
                async for tap in taps:
                    recorded = await labels.record(pool, tap)
                    await notifier.confirm(tap, recorded=recorded)
                    save()
                    if tap.alert_id == alert_id:
                        async with pool.connection() as conn:
                            row = await (
                                await conn.execute(
                                    "SELECT decision_id, applies, source, alert_id "
                                    "FROM label WHERE alert_id=%s",
                                    [alert_id],
                                )
                            ).fetchone()
                        print("Verified label:", row, flush=True)
                        # Explicitly resume once so getUpdates acknowledges the committed update.
                        await asyncio.wait_for(anext(taps), 55)
                        break
        except TimeoutError:
            print(
                "Poll ended; see the fixture for whether a human tap arrived.",
                flush=True,
            )
        finally:
            await taps.aclose()
            save()


async def complete_timeout(out):
    """Replay documented callback shapes after the real wait times out. No live send."""
    from kiroku.notify.telegram import parse_tap

    live = json.loads((out / "smoke.json").read_text())
    sent = next(c for c in live["calls"] if c["method"] == "sendMessage")
    message = sent["response"]["result"]
    alert_id = int(
        sent["request"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    )
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "<CALLBACK_QUERY_ID>",
            "from": {"id": "<CHAT_ID>", "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": message["message_id"],
                "date": message["date"],
                "chat": {"id": "<CHAT_ID>", "type": "private"},
            },
            "data": f"l:{alert_id}:y",
        },
    }
    calls = [
        {
            "origin": "synthesized from Telegram Bot API docs",
            "method": "getUpdates",
            "request": {"timeout": 50, "allowed_updates": ["callback_query"]},
            "response": {"ok": True, "result": [update]},
            "status": 200,
        }
    ]
    settings = Settings.load()
    chat = int(settings.secrets.require(SecretName.TELEGRAM_CHAT).reveal())
    restored = json.loads(json.dumps(update).replace('"<CHAT_ID>"', str(chat)))
    tap = parse_tap(restored, chat)
    assert tap is not None
    url = await scratch()
    async with open_pool(url) as pool:
        assert await labels.record(pool, tap)
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT decision_id,applies,source,alert_id FROM label WHERE alert_id=%s",
                    [alert_id],
                )
            ).fetchone()
        print("Synthesized callback verified through labels.record:", row)

    def response(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        if body.get("chat_id") == chat:
            body["chat_id"] = "<CHAT_ID>"
        reply = {"ok": True, "result": True}
        calls.append(
            {
                "origin": "synthesized from Telegram Bot API docs",
                "method": method,
                "request": body,
                "response": reply,
                "status": 200,
            }
        )
        return httpx.Response(200, json=reply)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
        notifier = TelegramNotifier(http, settings.secrets.require(SecretName.TELEGRAM), chat)
        await notifier.confirm(tap, recorded=True)
    (out / "synthesized.json").write_text(
        json.dumps(
            {
                "origin": "synthesized: no human tap within 20 minutes",
                "label": list(row),
                "calls": calls,
            },
            indent=2,
        )
    )


async def repair_prefix(out):
    """Correct the first-line marker on the existing test message, without a second send."""
    live = json.loads((out / "smoke.json").read_text())
    sent = next(c for c in live["calls"] if c["method"] == "sendMessage")
    settings = Settings.load()
    token = settings.secrets.require(SecretName.TELEGRAM)
    chat = int(settings.secrets.require(SecretName.TELEGRAM_CHAT).reveal())
    recorder = Recorder(token=token.reveal(), chat_id=chat)
    async with httpx.AsyncClient(transport=recorder) as http:
        bot = TelegramNotifier(http, token, chat)
        response = await bot._post(
            "editMessageText",
            {
                "chat_id": chat,
                "message_id": sent["response"]["result"]["message_id"],
                "text": "kiroku test:\n" + sent["request"]["text"].replace("kiroku test: ", ""),
                "parse_mode": "HTML",
                "reply_markup": sent["request"]["reply_markup"],
                "link_preview_options": {"is_disabled": True},
            },
            15.0,
        )
        print("Existing test message prefix corrected:", response.status_code == 200)
    live["calls"] += recorder.rows
    (out / "smoke.json").write_text(json.dumps(live, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path)
    p.add_argument("--complete-timeout", action="store_true")
    p.add_argument("--repair-prefix", action="store_true")
    a = p.parse_args()
    asyncio.run(
        repair_prefix(a.out)
        if a.repair_prefix
        else complete_timeout(a.out)
        if a.complete_timeout
        else main(a.out)
    )
