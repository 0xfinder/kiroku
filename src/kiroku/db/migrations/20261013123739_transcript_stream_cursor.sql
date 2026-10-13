-- transcript_stream_cursor
CREATE INDEX utterance_session_commit ON utterance (session_id, id) INCLUDE (audio_start_ms);
CREATE INDEX utterance_session_audio ON utterance (session_id, audio_start_ms, id);
