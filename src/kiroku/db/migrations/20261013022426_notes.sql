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
    status text NOT NULL CHECK (status IN ('pending', 'ok', 'empty', 'invalid', 'failed', 'over_budget')),
    schema_name text NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version > 0),
    body jsonb,
    input jsonb NOT NULL,
    outputs jsonb NOT NULL CHECK (jsonb_typeof(outputs) = 'array'),
    model text,
    system text NOT NULL,
    receipts uuid[] NOT NULL DEFAULT '{}',
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, start_ms),
    CHECK ((status = 'ok') = (body IS NOT NULL)),
    CHECK (body IS NULL OR jsonb_typeof(body) = 'object'),
    CHECK ((status IN ('pending', 'invalid', 'failed', 'over_budget')) = (reason IS NOT NULL)),
    CHECK (status NOT IN ('ok', 'invalid') OR model IS NOT NULL),
    CHECK (status != 'empty' OR model IS NULL)
);
CREATE INDEX note_created ON note(created_at, id);
