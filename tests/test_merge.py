"""Pure-function tests for segment merge (Spec 08, ADR-004).

No I/O, no model: merge_segments is a pure transform. The timestamp invariant
(merged boundaries come only from input boundaries) is enforced explicitly.
"""
from video_translate.merge import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_DUR,
    DEFAULT_MAX_GAP,
    _split_by_gap,
    drop_hallucination_segments,
    merge_segments,
    merge_short_cues,
    snap_drifted_words,
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


# --- V4: hallucination segment filter ---


def _seg(text, word_times):
    """word_times: list of (start, end) matching the tokens in text."""
    toks = text.split()
    return {
        "start": word_times[0][0], "end": word_times[-1][1], "text": text,
        "words": [{"word": " " + t, "start": s, "end": e}
                  for t, (s, e) in zip(toks, word_times)],
    }


def test_hallucination_collapsed_and_repeated_is_dropped():
    """The observed failure mode: 'at the end of the day, the movie is a movie'
    — most words collapsed onto one timestamp AND text repeats a neighbor."""
    real1 = _seg("But is it true that maybe at the end of the day, the",
                 [(62.0, 62.6), (62.6, 63.3), (63.3, 63.5), (63.5, 63.9),
                  (63.9, 64.3), (64.3, 64.6), (64.6, 64.7), (64.7, 64.8),
                  (64.8, 64.85), (64.85, 64.9), (64.9, 64.95)])
    halluc = _seg("at the end of the day, the movie is a movie",
                  [(64.6, 64.85), (64.85, 64.93), (64.93, 64.99)] +
                  [(64.99, 64.99)] * 8)  # 8 of 11 words collapsed
    real2 = _seg("and at the end of the day, it is about love?",
                 [(64.99, 65.0), (65.0, 65.1), (65.1, 65.3), (65.3, 65.5),
                  (65.5, 65.8), (65.8, 66.0), (66.0, 66.3), (66.3, 66.6),
                  (66.6, 67.0), (67.0, 67.6)])
    out = drop_hallucination_segments([real1, halluc, real2],
                                      progress=lambda *_: None)
    assert [s["text"] for s in out] == [real1["text"], real2["text"]]


def test_hallucination_collapse_without_repeat_is_kept():
    """Collapsed alignment alone is not proof of hallucination — keep it."""
    collapsed = _seg("something genuinely said here aloud",
                     [(10.0, 10.5)] + [(10.5, 10.5)] * 4)
    neighbor = _seg("a totally different sentence follows",
                    [(11.0 + i * 0.3, 11.3 + i * 0.3) for i in range(5)])
    out = drop_hallucination_segments([collapsed, neighbor],
                                      progress=lambda *_: None)
    assert len(out) == 2


def test_hallucination_repeat_without_collapse_is_kept():
    """Real repeated speech ('I agree. I agree.') must survive."""
    def timed(text, t0):
        toks = text.split()
        return _seg(text, [(t0 + i * 0.4, t0 + i * 0.4 + 0.3)
                           for i in range(len(toks))])
    a = timed("I agree.", 0.0)
    b = timed("I agree.", 1.0)  # genuine repetition, healthy alignment
    out = drop_hallucination_segments([a, b], progress=lambda *_: None)
    assert len(out) == 2


def test_hallucination_no_words_is_kept():
    """Segments without word timestamps cannot be judged — conservatively kept."""
    segs = [{"start": 0, "end": 1, "text": "the end of the day"},
            {"start": 1, "end": 2, "text": "the end of the day"}]
    out = drop_hallucination_segments(segs, progress=lambda *_: None)
    assert len(out) == 2


# --- V4: short-cue rejoin ---


def test_short_cue_rejoins_left_neighbor():
    """'love?' (0.4s orphan) rejoins '...it is about' — the readability fix."""
    segs = [
        {"start": 64.99, "end": 67.04, "text": "and at the end of the day, it is about",
         "words": [{"word": " and", "start": 64.99, "end": 65.2},
                   {"word": " about", "start": 66.8, "end": 67.04}]},
        {"start": 67.24, "end": 67.64, "text": "love?",
         "words": [{"word": " love?", "start": 67.24, "end": 67.64}]},
    ]
    out = merge_short_cues(segs)
    assert len(out) == 1
    assert out[0]["text"] == "and at the end of the day, it is about love?"
    assert out[0]["start"] == 64.99 and out[0]["end"] == 67.64
    assert len(out[0]["words"]) == 3


def test_short_cue_not_joined_after_sentence_end():
    """A drifted fragment must NOT be glued onto a finished sentence
    ('Resurrection.' + 'I've' would corrupt both)."""
    segs = [
        {"start": 9.88, "end": 10.72, "text": "Resurrection."},
        {"start": 11.14, "end": 11.66, "text": "I've",
         "words": [{"word": " I've", "start": 11.14, "end": 11.66}]},
    ]
    out = merge_short_cues(segs)
    assert len(out) == 2


def test_short_cue_not_joined_across_big_gap():
    segs = [
        {"start": 0.0, "end": 1.0, "text": "hello there"},
        {"start": 3.5, "end": 3.8, "text": "uh",  # gap 2.5s — genuinely standalone
         "words": [{"word": " uh", "start": 3.5, "end": 3.8}]},
    ]
    out = merge_short_cues(segs)
    assert len(out) == 2


def test_short_cue_boundaries_come_from_input():
    """Rejoined cue keeps the invariant: boundaries are input boundaries."""
    segs = [
        {"start": 10.0, "end": 11.0, "text": "it is about"},
        {"start": 11.2, "end": 11.5, "text": "love?"},
    ]
    out = merge_short_cues(segs)
    assert out[0]["start"] == 10.0 and out[0]["end"] == 11.5
    assert len(out) == 1


# --- V6 (B4): word-timestamp drift snap ---


def _dw(word, start, end):
    return {"word": word, "start": start, "end": end}


def _dseg(words):
    return {"start": words[0]["start"], "end": words[-1]["end"],
            "text": " ".join(w["word"].strip() for w in words),
            "words": list(words)}


def test_drift_snap_leading_orphan_word():
    """Real case: 'I' @4.95 belongs to 'pray you...' @17.82, 12.4s later."""
    words = [_dw(" I", 4.95, 5.47), _dw(" pray", 17.82, 18.36),
             _dw(" you", 18.36, 18.6), _dw(" back", 18.6, 21.28)]
    out = snap_drifted_words([_dseg(words)], progress=lambda *_: None)
    assert len(out) == 1
    assert out[0]["start"] == 17.82
    assert out[0]["words"][0]["start"] == 17.82
    # text and word order untouched
    assert [w["word"] for w in out[0]["words"]] == [" I", " pray", " you", " back"]


def test_drift_snap_prevents_orphan_cue_after_split():
    """End result of the bug: split no longer emits a lone 'Do' cue."""
    words = [_dw(" Do", 32.72, 33.16), _dw(" we", 40.71, 40.9),
             _dw(" have", 40.9, 41.1), _dw(" terms?", 41.1, 41.33)]
    snapped = snap_drifted_words([_dseg(words)], progress=lambda *_: None)
    out = split_long_cues(snapped, max_gap=1.0)
    assert len(out) == 1
    assert out[0]["text"].strip() == "Do we have terms?"
    assert out[0]["start"] == 40.71 and out[0]["end"] == 41.33


def test_drift_snap_leaves_real_pause_alone():
    """Issue #001's real 2.3s scene-break silence has multi-word groups on both
    sides — it must survive untouched."""
    words = [_dw("Scene", 0.0, 0.35), _dw("one", 0.4, 0.7), _dw("ends.", 0.75, 1.0),
             _dw("Scene", 3.3, 3.6), _dw("two", 3.7, 4.0), _dw("begins.", 4.1, 4.4)]
    out = snap_drifted_words([_dseg(words)], progress=lambda *_: None)
    assert out[0]["words"] == words


def test_drift_snap_keeps_standalone_sentence_word():
    """A lone word that ENDS a sentence ('Yes.') is a real utterance."""
    words = [_dw(" Yes.", 1.0, 1.4), _dw(" I", 9.0, 9.2), _dw(" agree", 9.2, 9.8)]
    out = snap_drifted_words([_dseg(words)], progress=lambda *_: None)
    assert out[0]["words"][0]["start"] == 1.0


def test_drift_snap_never_touches_trailing_group():
    """No following group to snap onto -> the tail is left as-is."""
    words = [_dw(" hello", 1.0, 1.5), _dw(" there", 1.5, 2.0), _dw(" ok", 9.0, 9.3)]
    out = snap_drifted_words([_dseg(words)], progress=lambda *_: None)
    assert out[0]["words"][-1]["start"] == 9.0


def test_drift_snap_respects_gap_threshold():
    """A 1.2s gap is below drift_gap=2.0 -> not drift, left alone."""
    words = [_dw(" I", 1.0, 1.4), _dw(" pray", 2.6, 3.0), _dw(" you", 3.0, 3.4)]
    out = snap_drifted_words([_dseg(words)], progress=lambda *_: None)
    assert out[0]["words"][0]["start"] == 1.0


def test_drift_snap_is_pure():
    words = [_dw(" I", 4.95, 5.47), _dw(" pray", 17.82, 18.36), _dw(" on", 18.4, 18.9)]
    seg = _dseg(words)
    snap_drifted_words([seg], progress=lambda *_: None)
    assert seg["words"][0]["start"] == 4.95 and seg["start"] == 4.95
