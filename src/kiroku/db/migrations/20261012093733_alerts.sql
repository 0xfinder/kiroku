-- Alerts and human Labels (#5).
CREATE TABLE alert (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id        bigint      NOT NULL UNIQUE REFERENCES gate_decision (id),
    -- Copied from the decision, its Window and session, so the Cooldown and Novelty lookups
    -- are single-table index scans.
    rule_id            bigint      NOT NULL REFERENCES rule (id),
    session_id         bigint      NOT NULL REFERENCES stream_session (id),
    channel_id         bigint      NOT NULL REFERENCES channel (id),
    said_at            timestamptz,          -- gate_window.started_at (wall clock of the speech)
    -- The Pack's Game key (gate_window.game_key), else the coarse 'seg:...' key. NULL only for
    -- stale rows, which are decided before a key is needed.
    game_key           text,
    outcome            text        NOT NULL CHECK (outcome IN
                           ('stale', 'cooldown', 'duplicate', 'sending', 'sent',
                            'unconfirmed', 'failed')),
    reason             text,                 -- why failed/unconfirmed/degraded; a Cooldown note
    -- {"claim", "summary", "quote"} from Extraction, or {"degraded", "excerpt"} without it.
    content            jsonb,
    extraction_model   text,                 -- 'groq:openai/gpt-oss-120b'; NULL when degraded
    previous_alert_id  bigint      REFERENCES alert (id),  -- what the Novelty check compared
    notifier           text,                 -- 'telegram'
    external_id        text,                 -- Telegram '<chat_id>:<message_id>'
    decided_at         timestamptz NOT NULL DEFAULT now(),
    sent_at            timestamptz,
    CHECK (outcome = 'stale' OR (said_at IS NOT NULL AND game_key IS NOT NULL)),
    CHECK (outcome IN ('stale', 'cooldown') OR content IS NOT NULL),
    CHECK (outcome <> 'duplicate' OR previous_alert_id IS NOT NULL),
    CHECK ((outcome IN ('sending', 'sent', 'unconfirmed', 'failed')) = (notifier IS NOT NULL)),
    CHECK ((outcome = 'sent') = (external_id IS NOT NULL AND sent_at IS NOT NULL)),
    CHECK (outcome NOT IN ('failed', 'unconfirmed') OR reason IS NOT NULL)
);
-- Cooldown: the latest Alert of a Rule on a Channel that may have reached the user.
CREATE INDEX alert_cooldown ON alert (rule_id, channel_id, said_at DESC)
    WHERE outcome IN ('sending', 'sent', 'unconfirmed');
-- Novelty reference and the coarse key's chain.
CREATE INDEX alert_game ON alert (rule_id, channel_id, game_key, said_at DESC)
    WHERE game_key IS NOT NULL;

-- A human judgment on a Gate decision (D15). Written only by kiroku.labels. One per
-- (decision, source): a second tap changes the verdict, and a redelivered Telegram update is
-- a no-op. Review-queue Labels (#8) use source 'review' and no alert.
CREATE TABLE label (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id  bigint      NOT NULL REFERENCES gate_decision (id),
    applies      boolean     NOT NULL,   -- 👍: the Rule really holds for the Window's `new`
    source       text        NOT NULL CHECK (source IN ('telegram', 'review')),
    alert_id     bigint      REFERENCES alert (id),
    labelled_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (decision_id, source),
    CHECK ((source = 'telegram') = (alert_id IS NOT NULL))
);
