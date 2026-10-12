# Telegram Alerts

A passing Gate decision produces one durable `alert` outcome. The worker handles decisions on the channels claimed by this process, including final Windows from closed sessions. It reads saved decisions and runs independently of capture and gating.

## Decision order

1. Reject speech older than ten minutes, using the capture leg holding the Window's end.
2. Use the Pack's Game key, or continue a coarse Segment key within a twenty-minute gap.
3. Check Cooldown for the Rule on that Channel, before spending on Extraction.
4. Extract from the saved Gate question and state. Compare with the previous delivered or possibly delivered claim for that Rule, Channel and Game key.
5. Store a duplicate when the take is unchanged. Failed or Budget-refused Extraction uses a degraded transcript excerpt.
6. Check freshness again, then commit `sending` and its ledger rows before sending one message.
7. Store `sent`, `failed`, or `unconfirmed` from the Notifier's result.

`kiroku alert list --last N` shows recent outcomes and suppression reasons. `kiroku watch --no-alerts` keeps Gate and Fallback evaluation without Telegram sending or polling. `--no-gate` transcribes only.

## Delivery and Labels

Delivery is at most once. A crash after `sending` commits but before Telegram receives the request can lose a message. A crash after Telegram receives it can lose the success receipt. Both recover to `unconfirmed`, with no resend. Telegram 5xx, uncertain transport failures and malformed success bodies have the same outcome. Only definite connect failures and valid 429 responses retry. The adapter paces sends to one per second.

Every message has 👍/👎 buttons naming its alert id. A tap locks the alert row and upserts one Label per decision and source. An uncertain delivery becomes `sent` in that transaction, using the callback's message date and external id. Commit precedes acknowledgement. Redelivered updates leave the Label unchanged, and an opposite tap changes the verdict. An advisory lock permits one Telegram poller; another watch waits to take over if the owner exits.

## Module ownership

| Module | Owns |
| --- | --- |
| `alerting.py` | Alert rows, suppression reads, recovery and worker |
| `labels.py` | Label rows and polling ownership |
| `extraction.py` | Pure prompt inputs and Extraction parsing |
| `llm/` | JSON completion interface and Groq boundary |
| `gate/fallback.py` | Primary outage bypass and combined costs |
| `gate/llm.py` | Same Gate questions through the JSON LLM |
| `notify/` | Notifier interface and Telegram boundary |
| `cli.py` | Provider composition, secrets and commands |

## Design choice and review

The arena selected candidate 1; the independent cross-judge agreed. Its process-wide worker handles closed-session Windows that candidate 2's session worker would miss. Candidate 3's extra attempt, cursor and ledger-identity tables were unnecessary for the single-user tool.

The synthesis grafts uncertain Telegram 5xx handling and callback delivery confirmation from candidate 3, and send pacing and `alert list` from candidate 2. Its corrections pin Extraction to the stored wording, use the final capture leg for freshness, and preserve primary costs when the Fallback cannot spend. D28 and D29 record the resulting choices.

Independent GPT-6-Astra, GPT-6-Sol and GPT-6-Luna reviews found bugs in quota expiry, concurrent quota admission, poller handoff, authentication-cost preservation, and per-decision failure recovery. The fixes have offline behavior tests. Stored Gate state follows the existing trusted-writer boundary: core code writes its shape, and manually corrupted database JSON can still stop candidate decoding.

## Measured limits

[The live recordings and comparisons](../spikes/README.md) hold the prompts and provider bodies to measured behavior. They do not calibrate the Fallback's probabilities to Jev's thresholds. The coarse key can combine two games within one Segment. Co-stream speakers remain mixed until Speaker mapping ships. A process killed between an LLM response and its transaction can incur an unrecorded bill and re-ask after restart. Actual said-to-sent latency during a pro match remains to be measured after merge.
