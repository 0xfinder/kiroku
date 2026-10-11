-- Issue #2: live transcript.

-- One capture leg is one streamlink|ffmpeg run that delivered audio. Each leg has its own
-- archive file (D23). Legs sit back to back on the session's audio clock, and each anchors
-- that clock to wall-clock time: audio at offset t in this leg was heard at
-- started_at + (t - audio_start_ms). utterance.spoken_at is derived from it at insert time.
-- Written only by kiroku.sessions, before any of the leg's audio reaches a Transcriber.
CREATE TABLE capture_leg (
    session_id      bigint      NOT NULL REFERENCES stream_session (id),
    seq             integer     NOT NULL CHECK (seq >= 1),
    audio_start_ms  integer     NOT NULL CHECK (audio_start_ms >= 0),
    audio_end_ms    integer,    -- NULL until the leg ends cleanly; NULL after a crash
    started_at      timestamptz NOT NULL,
    archive_path    text        NOT NULL,  -- relative to the data directory
    PRIMARY KEY (session_id, seq),
    CHECK (audio_end_ms IS NULL OR audio_end_ms >= audio_start_ms)
);

-- A range of session audio a Transcriber has finalized: every Utterance inside it is
-- stored and none will be added. Written only by kiroku.transcript, in one transaction with
-- its utterances and cost_ledger row, so a repeated commit is a no-op. Silent spans are
-- recorded too. max(audio_end_ms) is the "transcript complete up to here" watermark that
-- Windows (#3) read and that a restarted session resumes after.
CREATE TABLE transcript_span (
    session_id      bigint      NOT NULL REFERENCES stream_session (id),
    transcriber     text        NOT NULL,
    audio_start_ms  integer     NOT NULL,
    audio_end_ms    integer     NOT NULL,
    committed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, transcriber, audio_start_ms),
    CHECK (audio_end_ms > audio_start_ms)
);
