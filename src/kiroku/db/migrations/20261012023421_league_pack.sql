-- league_pack
ALTER TABLE gate_window ADD COLUMN game_key text;

CREATE TABLE segment (
    session_id bigint NOT NULL REFERENCES stream_session(id),
    start_ms integer NOT NULL CHECK (start_ms >= 0),
    title text NOT NULL,
    category text,
    pack text,
    PRIMARY KEY (session_id, start_ms)
);
