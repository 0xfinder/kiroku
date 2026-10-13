-- Notes owns this table. A terminal span and its paid calls commit exactly once.
CREATE TABLE note (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES stream_session(id),
    start_ms integer NOT NULL CHECK (start_ms >= 0),
    end_ms integer NOT NULL CHECK (end_ms > start_ms),
    transcriber text NOT NULL,
    pack text,
    mode text CHECK (mode IN ('watching_pro', 'playing', 'chatting', 'other')),
    game_key text,
    status text NOT NULL CHECK (status IN ('ok', 'empty', 'invalid', 'failed', 'over_budget')),
    schema_name text NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version > 0),
    body jsonb,
    input jsonb NOT NULL,
    outputs jsonb NOT NULL CHECK (jsonb_typeof(outputs) = 'array'),
    model text,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, start_ms),
    CHECK ((status = 'ok') = (body IS NOT NULL)),
    CHECK (body IS NULL OR jsonb_typeof(body) = 'object'),
    CHECK ((status IN ('invalid', 'failed', 'over_budget')) = (reason IS NOT NULL)),
    CHECK (status NOT IN ('ok', 'invalid') OR model IS NOT NULL),
    CHECK (status != 'empty' OR model IS NULL)
);
CREATE INDEX note_created ON note(created_at, id);

-- Non-terminal attempts must survive restart without advancing the Notes watermark.
-- Each entry commits with its own ledger rows; terminal note copies these outputs.
CREATE TABLE note_retry (
    id uuid PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES stream_session(id),
    start_ms integer NOT NULL,
    input jsonb NOT NULL,
    outputs jsonb NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX note_retry_span ON note_retry(session_id, start_ms);
