-- The tables issue #1 needs (PLAN.md, Storage). Conventions later migrations follow:
--   * bigint identity primary keys named id; foreign keys named <table>_id.
--   * timestamptz for wall-clock time, integer milliseconds for audio offsets.
--   * money as numeric, never float.
--   * open vocabularies (platform, provider, purpose) are text; the Python StrEnum is the list.

CREATE TABLE channel (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform    text    NOT NULL,
    login       text    NOT NULL,
    enabled     boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, login)
);

CREATE TABLE stream_session (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id          bigint NOT NULL REFERENCES channel (id),
    platform_stream_id  text,
    title               text,
    started_at          timestamptz NOT NULL,
    ended_at            timestamptz,
    audio_path          text,  -- relative to the data directory
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
-- At most one open session per channel, so a restart or reconnect resumes the open
-- session with INSERT ... ON CONFLICT instead of starting a second one.
CREATE UNIQUE INDEX stream_session_one_open_per_channel
    ON stream_session (channel_id) WHERE ended_at IS NULL;

CREATE TABLE utterance (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id       bigint  NOT NULL REFERENCES stream_session (id),
    -- Two clocks: the offset into the session's audio archive, and wall-clock time.
    -- A reconnect leaves a gap in the archive but not in wall-clock time, so neither
    -- can be derived from the other.
    audio_start_ms   integer NOT NULL,
    audio_end_ms     integer NOT NULL,
    spoken_at        timestamptz NOT NULL,
    speaker_label    text,  -- NULL when the Transcriber has no diarization
    text             text    NOT NULL,
    transcriber      text    NOT NULL,  -- e.g. 'groq:whisper-large-v3-turbo'
    CHECK (audio_end_ms >= audio_start_ms),
    -- Re-delivering a chunk is a no-op (ON CONFLICT DO NOTHING). Including the
    -- transcriber lets two Transcribers' output for the same audio coexist, for comparison.
    UNIQUE (session_id, transcriber, audio_start_ms)
);
CREATE INDEX utterance_session_time ON utterance (session_id, spoken_at);

CREATE TABLE cost_ledger (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    provider        text    NOT NULL,
    model           text    NOT NULL,
    purpose         text    NOT NULL,
    session_id      bigint  REFERENCES stream_session (id),  -- NULL outside a session
    input_tokens    integer,
    output_tokens   integer,
    audio_seconds   numeric(10, 3),
    usd             numeric(12, 6) NOT NULL CHECK (usd >= 0),
    latency_ms      integer NOT NULL,
    -- Billed by tokens or by audio, never both or neither. Mirrors kiroku.ledger.Usage.
    CHECK ((input_tokens IS NOT NULL) <> (audio_seconds IS NOT NULL)),
    CHECK (output_tokens IS NULL OR input_tokens IS NOT NULL)
);
CREATE INDEX cost_ledger_at ON cost_ledger (at);
CREATE INDEX cost_ledger_session ON cost_ledger (session_id) WHERE session_id IS NOT NULL;
