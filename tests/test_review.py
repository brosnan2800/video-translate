"""ADR-034 §6.2: dual-signal review — pure-function unit tests.

Every case is driven by SYNTHETIC signals (hand-built silence intervals and
no_speech_prob / avg_logprob values), so the whole verdict surface is covered
without ffmpeg, a model, or a video file.
"""
from video_translate.review import (
    CLEAN,
    HALLUCINATION,
    MISSING,
    _merge_intervals,
    _subtract,
    energy_fraction,
    find_word_uncovered_subwindows,
    is_energetic,
    review_segments,
    signal_a_state,
    slice_by_silence,
    word_coverage,
)


# --------------------------------------------------------------------------- #
# interval primitives
# --------------------------------------------------------------------------- #

def test_merge_intervals_sorts_and_coalesces():
    assert _merge_intervals([(5, 6), (0, 2), (1.5, 3)]) == [(0.0, 3.0), (5.0, 6.0)]


def test_merge_intervals_drops_degenerate():
    assert _merge_intervals([(2, 2), (3, 1)]) == []


def test_subtract_removes_middle():
    assert _subtract([(0, 10)], [(4, 6)]) == [(0.0, 4.0), (6.0, 10.0)]


def test_subtract_removes_multiple_cuts():
    assert _subtract([(0, 10)], [(2, 3), (7, 8)]) == [
        (0.0, 2.0), (3.0, 7.0), (8.0, 10.0)
    ]


def test_subtract_disjoint_cut_keeps_whole():
    assert _subtract([(0, 10)], [(20, 30)]) == [(0.0, 10.0)]


def test_subtract_full_cover_yields_empty():
    assert _subtract([(0, 10)], [(0, 10)]) == []


def test_subtract_multiple_pieces():
    assert _subtract([(0, 4), (6, 10)], [(2, 8)]) == [(0.0, 2.0), (8.0, 10.0)]


# --------------------------------------------------------------------------- #
# signal B — energy from the independent silencedetect reference
# --------------------------------------------------------------------------- #

def test_energy_fraction_bounds():
    assert energy_fraction(0, 10, []) == 1.0          # no silence -> all energy
    assert energy_fraction(0, 10, [(0, 10)]) == 0.0   # all silence
    assert energy_fraction(0, 10, [(0, 5)]) == 0.5


def test_energy_fraction_degenerate_window():
    assert energy_fraction(5, 5, []) == 0.0
    assert energy_fraction(7, 3, []) == 0.0


def test_energy_fraction_never_negative_on_overlapping_silence():
    # malformed/overlapping silencedetect output must not produce < 0
    assert energy_fraction(0, 4, [(0, 3), (1, 4)]) == 0.0


def test_is_energetic_requires_both_gates():
    # long window, tiny sliver of energy -> fraction gate rejects
    assert is_energetic(0, 100, [(0, 99.9)], min_frac=0.25, min_abs=0.25) is False
    # short burst of energy but below the absolute floor -> rejected
    assert is_energetic(0, 1, [(0, 0.8)], min_frac=0.25, min_abs=0.25) is False
    # genuinely energetic
    assert is_energetic(0, 10, [(5, 10)], min_frac=0.25, min_abs=0.25) is True


def test_is_energetic_degenerate_window_is_false():
    assert is_energetic(4, 4, []) is False


# --------------------------------------------------------------------------- #
# signal A — Whisper's own 2-D confidence
# --------------------------------------------------------------------------- #

def test_signal_a_high_no_speech_is_suspect():
    a = signal_a_state({"no_speech_prob": 0.9, "avg_logprob": -0.2})
    assert a["suspect"] is True
    assert "high_no_speech_prob" in a["reasons"]
    assert "low_avg_logprob" not in a["reasons"]


def test_signal_a_low_logprob_is_suspect():
    a = signal_a_state({"no_speech_prob": 0.1, "avg_logprob": -1.5})
    assert a["suspect"] is True
    assert a["reasons"] == ["low_avg_logprob"]


def test_signal_a_both_fire_independently():
    a = signal_a_state({"no_speech_prob": 0.8, "avg_logprob": -1.2})
    assert a["suspect"] is True
    assert len(a["reasons"]) == 2


def test_signal_a_clean():
    a = signal_a_state({"no_speech_prob": 0.2, "avg_logprob": -0.3})
    assert a["suspect"] is False
    assert a["reasons"] == []


def test_signal_a_missing_fields_is_unknown_not_clean():
    """Merged main-pass segments lose confidence fields -> unknown, NOT healthy.
    The verdict layer must not read this as 'clean' evidence."""
    a = signal_a_state({"text": "hello"})
    assert a["suspect"] is False
    assert a["reasons"] == []
    assert a["no_speech_prob"] is None and a["avg_logprob"] is None


def test_signal_a_boundaries_are_inclusive_for_no_speech_exclusive_for_logprob():
    assert signal_a_state({"no_speech_prob": 0.6})["suspect"] is True   # >=
    assert signal_a_state({"no_speech_prob": 0.59})["suspect"] is False
    assert signal_a_state({"avg_logprob": -1.0})["suspect"] is False    # < only
    assert signal_a_state({"avg_logprob": -1.01})["suspect"] is True


# --------------------------------------------------------------------------- #
# the verdict table (ADR-034 §3.1)
# --------------------------------------------------------------------------- #

def test_review_energetic_plus_bad_a_is_missing():
    segs = [{"start": 0, "end": 10, "text": "hi", "no_speech_prob": 0.9}]
    out = review_segments(segs, [])
    assert len(out) == 1
    assert out[0]["verdict"] == MISSING
    assert out[0]["energetic"] is True
    assert out[0]["reasons"] == ["high_no_speech_prob"]


def test_review_energetic_plus_low_logprob_is_missing():
    segs = [{"start": 0, "end": 10, "text": "hi", "avg_logprob": -1.5}]
    out = review_segments(segs, [])
    assert out and out[0]["verdict"] == MISSING


def test_review_silent_window_with_text_is_hallucination():
    segs = [{"start": 0, "end": 10, "text": "Thank you."}]
    out = review_segments(segs, [(0, 10)])
    assert len(out) == 1
    assert out[0]["verdict"] == HALLUCINATION
    assert out[0]["energetic"] is False


def test_review_energetic_and_clean_a_is_not_reported():
    segs = [{"start": 0, "end": 10, "text": "hi",
             "no_speech_prob": 0.1, "avg_logprob": -0.2}]
    assert review_segments(segs, []) == []


def test_review_silent_window_without_text_is_not_reported():
    # silence + no text = genuinely empty; nothing to flag
    assert review_segments([{"start": 0, "end": 10, "text": "  "}],
                           [(0, 10)]) == []


def test_review_unknown_a_on_energetic_window_reports_nothing():
    """A unknown + B energetic -> neither verdict applies. B-driven recall is
    G2's job (word coverage), not the A-gated MISSING verdict."""
    segs = [{"start": 0, "end": 10, "text": "hi"}]
    assert review_segments(segs, []) == []


def test_review_skips_segments_without_timestamps():
    segs = [{"text": "no times", "no_speech_prob": 0.99},
            {"start": 0, "end": 5, "no_speech_prob": 0.99},
            {"start": 5, "end": 5, "text": "zero length",
             "no_speech_prob": 0.99}]
    out = review_segments(segs, [])
    assert [r["index"] for r in out] == [1]


def test_review_missing_carries_g1_windows():
    segs = [{"start": 0, "end": 10, "text": "hi", "no_speech_prob": 0.9}]
    out = review_segments(segs, [(4, 6)])
    assert out[0]["g1_windows"] == [(0.0, 4.0), (6.0, 10.0)]


# --------------------------------------------------------------------------- #
# G1 — slice a suspect window at silence boundaries
# --------------------------------------------------------------------------- #

def test_slice_by_silence_splits_on_inner_silence():
    assert slice_by_silence(0, 10, [(4, 6)]) == [(0.0, 4.0), (6.0, 10.0)]


def test_slice_by_silence_no_inner_silence_yields_single_piece():
    """No silence gap -> G1 cannot cut (ADR-034 §3.2); caller falls to G2/G3."""
    assert slice_by_silence(0, 10, []) == [(0.0, 10.0)]
    assert slice_by_silence(0, 10, [(20, 30)]) == [(0.0, 10.0)]


def test_slice_by_silence_drops_pieces_below_min_sub():
    # only 0.5s of energy at either edge -> nothing usable to decode
    assert slice_by_silence(0, 10, [(0.5, 9.5)], min_sub=1.0) == []


def test_slice_by_silence_degenerate():
    assert slice_by_silence(5, 5, []) == []
    assert slice_by_silence(9, 3, []) == []


# --------------------------------------------------------------------------- #
# G2 — intra-segment word-uncovered energetic sub-windows
# --------------------------------------------------------------------------- #

def test_word_coverage_merges_adjacent_words():
    seg = {"words": [{"start": 0, "end": 1}, {"start": 1, "end": 2},
                     {"start": 5, "end": 6}]}
    assert word_coverage(seg) == [(0.0, 2.0), (5.0, 6.0)]


def test_g2_finds_energetic_gap_between_words():
    seg = {
        "start": 0, "end": 10,
        "words": [{"start": 0, "end": 2}, {"start": 3, "end": 5}],
    }
    # span minus words -> (2,3) 1.0s [below min_dur] and (5,10); minus silence
    # (7,10) -> (5,7) 2.0s of real energy.
    assert find_word_uncovered_subwindows(seg, [(7, 10)], min_dur=1.5) == [
        (5.0, 7.0)
    ]


def test_g2_ignores_gaps_below_min_dur():
    seg = {"start": 0, "end": 10,
           "words": [{"start": 0, "end": 2}, {"start": 3, "end": 5}]}
    # (5,10) is a gap, but silence starts at 5.5 -> only 0.5s energetic
    assert find_word_uncovered_subwindows(seg, [(5.5, 10)], min_dur=1.5) == []


def test_g2_fully_covered_segment_has_no_subwindows():
    seg = {"start": 0, "end": 4,
           "words": [{"start": 0, "end": 2}, {"start": 2, "end": 4}]}
    assert find_word_uncovered_subwindows(seg, []) == []


def test_g2_silent_gap_is_not_a_subwindow():
    """A pause between clauses is silence, not lost speech."""
    seg = {"start": 0, "end": 10,
           "words": [{"start": 0, "end": 2}, {"start": 8, "end": 10}]}
    assert find_word_uncovered_subwindows(seg, [(2, 8)], min_dur=1.5) == []


def test_g2_segment_without_words_is_skipped():
    """No word timestamps -> coverage is unknowable; do NOT re-decode the whole
    segment on suspicion alone (too large a rewrite)."""
    seg = {"start": 0, "end": 10, "text": "hi"}
    assert find_word_uncovered_subwindows(seg, []) == []


def test_g2_missing_or_degenerate_timespan_yields_nothing():
    assert find_word_uncovered_subwindows({"text": "x"}, []) == []
    assert find_word_uncovered_subwindows({"start": 5, "end": 5,
                                           "words": [{"start": 0, "end": 1}]},
                                          []) == []


def test_g2_reports_multiple_subwindows():
    seg = {"start": 0, "end": 20,
           "words": [{"start": 0, "end": 2}, {"start": 6, "end": 8},
                     {"start": 12, "end": 14}]}
    got = find_word_uncovered_subwindows(seg, [], min_dur=1.5)
    assert got == [(2.0, 6.0), (8.0, 12.0), (14.0, 20.0)]
