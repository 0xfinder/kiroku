-- Issue #3: Rules, Windows, Gate decisions.

-- A natural-language alert condition (CONTEXT.md). Created here rather than in #4, because
-- gate_decision references it and the Gate reads the active set; #4 adds the CLI over these
-- columns. Written only by kiroku.rules. Rules are disabled, never deleted: gate_decision
-- (and later label) rows reference them.
CREATE TABLE rule (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner         text         NOT NULL DEFAULT 'me',  -- D2
    name          text         NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,62}$'),
    -- The yes/no condition as the user wrote it, e.g. "Does the streamer predict which team
    -- will win the current game or series?". The Gate wraps it (kiroku.gate.rule_question),
    -- and only that wrapper names state fields: backticks would point Jev at them.
    instructions  text         NOT NULL CHECK (length(btrim(instructions)) BETWEEN 1 AND 2000
                                               AND position('`' IN instructions) = 0),
    pack          text,        -- NULL: applies with any Pack and with none
    threshold     numeric(3,2) NOT NULL CHECK (threshold > 0 AND threshold <= 1),
    cooldown_s    integer      NOT NULL DEFAULT 600 CHECK (cooldown_s >= 0),  -- read by #5
    enabled       boolean      NOT NULL DEFAULT true,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (owner, name)
);

-- One Window: the slice [start_ms, end_ms) of the session audio clock and what the Gate made
-- of it. Windows tile the clock with no gap or overlap: each starts at the previous one's end.
-- Stored as gate_window because WINDOW is a reserved word in Postgres. Written only by
-- kiroku.gating, in one transaction with its gate_decision and cost_ledger rows, so the
-- (session_id, start_ms) key makes a repeated commit a no-op. A row is final when written: a
-- Window is gated once and never re-gated, backfill included.
CREATE TABLE gate_window (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id        bigint       NOT NULL REFERENCES stream_session (id),
    start_ms          integer      NOT NULL CHECK (start_ms >= 0),
    end_ms            integer      NOT NULL,
    transcriber       text         NOT NULL,  -- whose utterances and spans it read
    -- Wall-clock time of start_ms, from the capture leg holding it. NULL when no leg holds it
    -- (audio lost before a crash, never captured).
    started_at        timestamptz,
    -- Milliseconds of [start_ms, end_ms) inside committed transcript spans when it closed.
    -- Less than the length means a gap (crash, Budget cap). Backfill does not update it.
    covered_ms        integer      NOT NULL,
    -- Exactly what the Gate saw, or would have seen: {"context": {...}, "new": "..."}.
    -- #4 replays Rules against it; #5 Extraction reads it.
    state             jsonb        NOT NULL,
    status            text         NOT NULL
        CHECK (status IN ('answered', 'empty', 'over_budget', 'failed')),
    reason            text,        -- why not answered; or why a Fallback answered (#5)
    model             text,        -- e.g. 'jev:jev-1.13.0' as reported; NULL unless answered
    mode              text,        -- kiroku.gate.Mode
    mode_probability  numeric(5,4) CHECK (mode_probability BETWEEN 0 AND 1),
    closed_at         timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (session_id, start_ms),
    CHECK (end_ms > start_ms),
    CHECK (covered_ms BETWEEN 0 AND end_ms - start_ms),
    CHECK ((status = 'answered') = (model IS NOT NULL)),
    CHECK (status = 'answered' OR (mode IS NULL AND mode_probability IS NULL)),
    CHECK (status IN ('answered', 'empty') OR reason IS NOT NULL)
);

-- One (Window, Rule) result: exactly one per Rule that was active when the Window was gated,
-- whatever the Window's status. probability is NULL when the Rule was not asked (empty,
-- over_budget, failed). question and threshold are what was actually used, so editing a Rule
-- never rewrites history, and Labels (#5) judge the wording that produced the decision.
CREATE TABLE gate_decision (
    id           bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    window_id    bigint       NOT NULL REFERENCES gate_window (id),
    rule_id      bigint       NOT NULL REFERENCES rule (id),
    question     text         NOT NULL,
    threshold    numeric(3,2) NOT NULL,
    probability  numeric(5,4) CHECK (probability BETWEEN 0 AND 1),
    -- Computed by Postgres, so Python and SQL can never disagree at the boundary.
    passed       boolean      GENERATED ALWAYS AS (coalesce(probability >= threshold, false)) STORED,
    UNIQUE (window_id, rule_id)
);
CREATE INDEX gate_decision_rule ON gate_decision (rule_id, window_id);
-- #5 Extraction reads passing decisions.
CREATE INDEX gate_decision_passed ON gate_decision (window_id) WHERE passed;
