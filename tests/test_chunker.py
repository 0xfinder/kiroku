import itertools

from kiroku.audio import SAMPLES_PER_MS
from kiroku.transcriber.chunker import Chunk, ChunkPolicy, SilenceChunker
from tests.fakes.speech import speechlike, tone

POLICY = ChunkPolicy()


def _chunk_all(pcm: bytes, start_sample: int = 0, feed_bytes: int = 7_001) -> list[Chunk]:
    """Feed in odd-sized pieces, like a pipe delivers them."""
    chunker = SilenceChunker(start_sample, POLICY)
    out: list[Chunk] = []
    for i in range(0, len(pcm), feed_bytes):
        out += chunker.feed(pcm[i : i + feed_bytes])
    return out + chunker.flush()


def _cut_ms(chunks: list[Chunk]) -> list[int]:
    return [c.end_sample // SAMPLES_PER_MS for c in chunks[:-1]]


def test_chunks_tile_the_input_exactly() -> None:
    pcm, _ = speechlike(120, seed=1)
    chunks = _chunk_all(pcm, start_sample=16_000)
    assert b"".join(c.pcm for c in chunks) == pcm
    assert chunks[0].start_sample == 16_000
    for a, b in itertools.pairwise(chunks):
        assert b.start_sample == a.end_sample


def test_cuts_land_in_pauses_within_length_bounds() -> None:
    pcm, pauses = speechlike(180, seed=2)
    chunks = _chunk_all(pcm)
    for cut in _cut_ms(chunks):
        assert any(start <= cut <= end for start, end in pauses), f"cut at {cut} ms splits speech"
    for c in chunks[:-1]:
        length_ms = (c.end_sample - c.start_sample) // SAMPLES_PER_MS
        # A pause cut lands mid-pause, so up to half a pause before the minimum.
        assert POLICY.min_ms - POLICY.pause_ms // 2 <= length_ms <= POLICY.max_ms + 20


def test_cuts_still_find_pauses_over_a_noise_bed() -> None:
    pcm, pauses = speechlike(180, seed=3, noise_db=-40)
    chunks = _chunk_all(pcm)
    in_pause = [any(s <= cut <= e for s, e in pauses) for cut in _cut_ms(chunks)]
    assert len(in_pause) >= 5
    assert all(in_pause)


def test_audio_without_pauses_is_cut_at_the_maximum() -> None:
    chunks = _chunk_all(tone(80))
    for c in chunks[:-1]:
        assert (c.end_sample - c.start_sample) // SAMPLES_PER_MS <= POLICY.max_ms
    assert len(chunks) >= 3


def test_digital_silence_is_not_voiced() -> None:
    chunks = _chunk_all(bytes(16_000 * 2 * 40))
    assert chunks
    assert not any(c.voiced for c in chunks)


def test_speech_is_voiced() -> None:
    pcm, _ = speechlike(40, seed=4)
    assert all(c.voiced for c in _chunk_all(pcm))
