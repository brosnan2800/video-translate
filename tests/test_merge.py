"""Pure-function tests for segment merge (Spec 08, ADR-004).

No I/O, no model: merge_segments is a pure transform. The timestamp invariant
(merged boundaries come only from input boundaries) is enforced explicitly.
"""
from video_translate.merge import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_DUR,
    DEFAULT_MAX_GAP,
    _split_by_gap,
    merge_segments,
    split_long_cues,
)


# --- invariant: timestamps never recomputed ---


def test_merged_start_is_first_segment_start():
    segs = [{"start": 0, "end": 1, "text": "a"}, {"start": 1.1, "end": 2, "text": "b"}]
    merged = merge_segments(segs, max_gap=1.0)
    assert merged[0]["start"] == 0  # equals first input start


def test_merged_end_is_last_segment_end():
    segs = [{"start": 0, "end": 1, "text": "a"}, {"start": 1.1, "end": 2.5, "text": "b"}]
    merged = merge_segments(segs, max_gap=1.0)
    assert merged[0]["end"] == 2.5  # equals last input end


def test_all_boundary_values_come_from_input():
    """Every merged start/end must appear in the input start/end sets."""
    segs = [{"start": s, "end": e, "text": f"t{i}"}
            for i, (s, e) in enumerate([(0, 1), (1.1, 2), (2.1, 3), (10, 11), (11.1, 12)])]
    merged = merge_segments(segs)
    in_starts = {s["start"] for s in segs}
    in_ends = {s["end"] for s in segs}
    for m in merged:
        assert m["start"] in in_starts
        assert m["end"] in in_ends


# --- rules ---


def test_gap_exceeds_threshold_no_merge():
    segs = [{"start": 0, "end": 1, "text": "a"}, {"start": 2, "end": 3, "text": "b"}]  # gap=1.0
    assert len(merge_segments(segs, max_gap=0.5)) == 2


def test_max_dur_caps_merge():
    # 5 segments of 2s each, contiguous; max_dur=4 -> 3 groups
    segs = [{"start": i * 2, "end": (i + 1) * 2, "text": f"s{i}"} for i in range(5)]
    merged = merge_segments(segs, max_dur=4.0, max_gap=1.0)
    assert len(merged) == 3
    assert merged[0]["end"] - merged[0]["start"] <= 4.0


def test_fragments_rejoin_regardless_of_text_length():
    """max_chars is NOT a merge gate (reserved for v3 split); long fragments
    still rejoin within the duration budget."""
    segs = [{"start": i, "end": i + 0.9, "text": "x" * 30} for i in range(3)]
    merged = merge_segments(segs, max_gap=1.0, max_dur=8.0)
    assert len(merged) == 1  # all 3 rejoin (gap<0.5, dur<=8, no sentence end)
    assert DEFAULT_MAX_CHARS == 42  # constant still exposed (reserved for v3)


def test_sentence_end_breaks_group():
    segs = [{"start": 0, "end": 1, "text": "Hello."}, {"start": 1.1, "end": 2, "text": "World"}]
    merged = merge_segments(segs, max_gap=1.0, respect_sentence_end=True)
    assert len(merged) == 2  # "Hello." ends a sentence -> no merge


def test_respect_sentence_end_false_merges_across_period():
    segs = [{"start": 0, "end": 1, "text": "Hello."}, {"start": 1.1, "end": 2, "text": "World"}]
    merged = merge_segments(segs, max_gap=1.0, respect_sentence_end=False)
    assert len(merged) == 1


def test_empty_input_returns_empty():
    assert merge_segments([]) == []


def test_single_segment_passthrough():
    segs = [{"start": 0, "end": 1, "text": "only"}]
    assert merge_segments(segs) == segs


def test_input_not_mutated():
    segs = [{"start": 0, "end": 1, "text": "a"}, {"start": 1.1, "end": 2, "text": "b"}]
    snapshot = [dict(s) for s in segs]
    merge_segments(segs, max_gap=1.0)
    assert segs == snapshot


def test_text_joined_with_single_space():
    segs = [{"start": 0, "end": 1, "text": "hello"}, {"start": 1.1, "end": 2, "text": "world"}]
    merged = merge_segments(segs, max_gap=1.0)
    assert merged[0]["text"] == "hello world"


def test_text_stripped_before_join():
    segs = [{"start": 0, "end": 1, "text": "  hello  "}, {"start": 1.1, "end": 2, "text": "  world  "}]
    merged = merge_segments(segs, max_gap=1.0)
    assert merged[0]["text"] == "hello world"


def test_defaults_exposed():
    assert DEFAULT_MAX_DUR == 8.0
    assert DEFAULT_MAX_GAP == 0.5


# --- V3: word-level carry + tightening + split ---


def test_emit_carries_words():
    """Words from fragments are concatenated and the cue is tightened to the
    first/last word boundary (drops VAD-padded leading silence)."""
    segs = [
        {"start": 0.0, "end": 1.0, "text": "Hello",
         "words": [{"word": "Hello", "start": 0.1, "end": 0.5}]},
        {"start": 1.1, "end": 2.0, "text": "world",
         "words": [{"word": "world", "start": 1.2, "end": 1.8}]},
    ]
    merged = merge_segments(segs, max_gap=1.0)
    assert "words" in merged[0]
    assert merged[0]["words"][0]["word"] == "Hello"
    assert merged[0]["words"][-1]["word"] == "world"
    # tightened: segment-level 0.0 -> first word 0.1
    assert merged[0]["start"] == 0.1
    assert merged[0]["end"] == 1.8
    assert merged[0]["text"] == "Hello world"


def test_emit_tightens_to_word_boundaries():
    """A single segment whose level start carries leading silence is tightened
    to the first word's start (fixes the 'cue appears early' symptom)."""
    segs = [{
        "start": 0.0, "end": 2.0, "text": "Hi there",
        "words": [{"word": "Hi", "start": 0.3, "end": 0.6},
                  {"word": "there", "start": 0.7, "end": 1.5}],
    }]
    merged = merge_segments(segs)
    assert merged[0]["start"] == 0.3   # silence dropped
    assert merged[0]["end"] == 1.5     # real last word end


def test_emit_without_words_has_no_words_key():
    """V2 payloads (no words) stay byte-compatible: no 'words' key is added."""
    segs = [{"start": 0, "end": 1, "text": "only"}]
    merged = merge_segments(segs)
    assert "words" not in merged[0]


def test_split_long_cue_by_words():
    """An over-long merged cue is split at word boundaries (never inside a word)."""
    words = [
        {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
        for i, w in enumerate(
            "the quick brown fox jumps over the lazy dog and then some more words here".split())
    ]
    text = " ".join(w["word"] for w in words)
    seg = {"start": words[0]["start"], "end": words[-1]["end"],
           "text": text, "words": words}
    out = split_long_cues([seg], max_chars=10)
    assert len(out) > 1
    for s in out:
        # the algorithm keeps each group's word-char sum (no spaces) <= max_chars
        # and never breaks a word; a group may display slightly longer due to the
        # joining spaces, but no single word is split.
        assert sum(len(w["word"]) for w in s["words"]) <= 10
        assert s["start"] == s["words"][0]["start"]
        assert s["end"] == s["words"][-1]["end"]
    # no word lost, order preserved
    flat = [w for s in out for w in s["words"]]
    assert flat == words


def test_no_split_keeps_whole():
    seg = {"start": 0, "end": 2, "text": "short",
           "words": [{"word": "short", "start": 0.1, "end": 1.9}]}
    assert split_long_cues([seg], enabled=False) == [seg]


def test_split_preserves_real_silence():
    """split_long_cues must not invent gaps; a real silence stays between cues."""
    words = [
        {"word": "a", "start": 0.0, "end": 0.3},
        {"word": "b", "start": 0.4, "end": 0.7},
        {"word": "c", "start": 3.0, "end": 3.3},  # 2.3s real silence
    ]
    seg = {"start": 0.0, "end": 3.3,
           "text": "a b c", "words": words}
    out = split_long_cues([seg], max_gap=1.0)
    assert len(out) == 2
    assert out[1]["start"] - out[0]["end"] > 1.0


def test_split_by_gap_word_boundary_only():
    words = [
        {"word": "a", "start": 0.0, "end": 0.3},
        {"word": "b", "start": 0.4, "end": 0.7},
        {"word": "c", "start": 3.0, "end": 3.3},
    ]
    groups = _split_by_gap(words, max_gap=1.0)
    assert len(groups) == 2
    assert [w["word"] for w in groups[0]] == ["a", "b"]
    assert [w["word"] for w in groups[1]] == ["c"]
