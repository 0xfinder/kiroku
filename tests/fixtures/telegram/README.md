# Telegram fixtures

`smoke.json` records one real Bot API `sendMessage` and its subsequent `getUpdates` polls. The existing message was edited to place `kiroku test:` on its first line and uses the production HTML renderer and 👍/👎 keyboard. No human tap arrived within the 20-minute wait on 2026-10-12. The token and destination chat id are placeholders; personal names and usernames are removed. It contains no token-bearing URL or authorization header.

`synthesized.json` contains the [Bot API documented](https://core.telegram.org/bots/api#callbackquery) callback shape and successful `answerCallbackQuery` and `editMessageReplyMarkup` shapes. These were replayed through the production Telegram parser, `labels.record` and `TelegramNotifier.confirm`, including a verified Label row in the scratch database. They are synthesized, not evidence of a human tap or a live acknowledgement. The original test message was not resent.

Boundary tests synthesize 429 with `parameters.retry_after`, blocked-bot 403, malformed bodies, and uncertain 5xx responses according to the [Bot API response contract](https://core.telegram.org/bots/api#making-requests). `tests/fakes/telegram.py` serves the recorded Message and documented callback shapes, and retains each update until a later `getUpdates` offset acknowledges it.
