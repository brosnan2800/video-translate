"""Tests for the ADR-012 acoustic-reference features (Spec 18).

Covers: audio_profile parsing + VAD routing, verify lanes (pure functions),
and the two silence-based hallucination guards (merge + fill_gaps). All pure —
no ffmpeg / model required.
"""
from video_translate.audio_profile import (
    AudioProfile, parse_silencedetect, parse_volumedetect, recommend_vad,
    route_vad_chunk, _silence_fraction, CLEAN_SILENCE_FRACTION,
)
from video_translate.verify import (
    CROSS_SILENCE, FIRST_CUE_EARLY, IN_SILENCE, cue_bounds, cue_cross_silence,
    cue_in_silence, first_cue_early, find_uncovered_speech, verify_acoustic,
    verify_presentation, find_untranslated_latin_words, build_semantic_reread_task,
)
from video_translate.merge import _in_silence_window, drop_hallucination_segments
from video_translate.fill_gaps import _hole_in_silence


# --------------------------- audio_profile ---------------------------

def test_parse_volumedetect_both():
    stderr = "[Parsed_volumedetect_0] mean_volume: -20.9 dB\n" \
             "[Parsed_volumedetect_0] max_volume: -5.3 dB\n"
    assert parse_volumedetect(stderr) == (-20.9, -5.3)


def test_parse_volumedetect_missing():
    assert parse_volumedetect("no volume lines here") == (None, None)


def test_parse_silencedetect_pairs():
    stderr = "silence_start: 0.0\nsilence_end: 6.96 | silence_duration: 6.96\n" \
             "silence_start: 12.56\nsilence_end: 14.21 | silence_duration: 1.65\n"
    assert parse_silencedetect(stderr) == [(0.0, 6.96), (12.56, 14.21)]


def test_parse_silencedetect_trailing_start_closes_at_duration():
    stderr = "silence_start: 131.20\n"  # no matching end (silence to EOF)
    assert parse_silencedetect(stderr, duration=140.67) == [(131.20, 140.67)]


def test_recommend_vad_low_level():
    prof = AudioProfile(mean_vol=-25.0, max_vol=-6.0, ok=True)
    flag, _ = recommend_vad(prof)
    assert flag == "--vad --vad-threshold 0.1"


def test_recommend_vad_clean():
    prof = AudioProfile(mean_vol=-16.0, max_vol=0.0, ok=True)
    flag, _ = recommend_vad(prof)
    assert flag == "--vad"


def test_recommend_vad_unavailable():
    prof = AudioProfile(ok=False)
    flag, _ = recommend_vad(prof)
    assert flag == "bare"


# --------------------------- verify: acoustic lane ---------------------------

def test_cue_in_silence():
    silences = [(0.0, 2.0), (8.0, 10.0)]
    assert cue_in_silence(0.5, 1.5, silences) is True
    assert cue_in_silence(3.0, 4.0, silences) is False


def test_cue_cross_silence():
    # a real pause (2.0s) strictly inside the cue -> drift across silence
    assert cue_cross_silence(7.0, 11.0, [(8.0, 10.0)]) is True
    # silence only overlaps the cue edge (not contained) -> not flagged
    assert cue_cross_silence(7.0, 9.0, [(8.0, 10.0)]) is False
    # contained but too short (< min_cross_dur default 1.0) -> not flagged
    assert cue_cross_silence(7.0, 9.0, [(7.5, 8.2)]) is False
    # no silence near -> not flagged
    assert cue_cross_silence(3.0, 4.0, [(8.0, 10.0)]) is False


def test_first_cue_early():
    silences = [(0.0, 6.96), (12.0, 14.0)]
    assert first_cue_early(5.0, silences) is True     # before real speech @6.96
    assert first_cue_early(7.0, silences) is False    # after
    assert first_cue_early(1.0, []) is False          # no leading silence
    assert first_cue_early(1.0, [(3.0, 5.0)]) is False  # leading silence not at 0


def test_verify_acoustic_flags_each_branch():
    segments = [
        {"start": 0.5, "end": 1.5, "words": [{"start": 0.5, "end": 1.5}]},  # in silence
        {"start": 7.0, "end": 11.0, "words": [{"start": 7.0, "end": 11.0}]},  # crosses real pause (8,10)
        {"start": 20.0, "end": 21.0, "words": [{"start": 20.0, "end": 21.0}]},  # clean
    ]
    silences = [(0.0, 2.0), (8.0, 10.0)]
    issues = verify_acoustic(segments, silences)
    types = {it["type"] for it in issues}
    assert IN_SILENCE in types
    assert CROSS_SILENCE in types
    assert FIRST_CUE_EARLY in types
    # the clean cue (#2, 20-21) is not flagged
    assert all(it["index"] != 2 for it in issues)


def test_verify_acoustic_empty_when_no_silence():
    segments = [{"start": 1.0, "end": 2.0, "words": [{"start": 1.0, "end": 2.0}]}]
    assert verify_acoustic(segments, []) == []


def test_cue_bounds_word_preferred():
    seg = {"start": 0.0, "end": 5.0, "words": [{"start": 1.0, "end": 4.0}]}
    assert cue_bounds([seg]) == [(1.0, 4.0)]
    seg2 = {"start": 0.0, "end": 5.0}
    assert cue_bounds([seg2]) == [(0.0, 5.0)]


# --------------------------- verify: presentation lane ---------------------------

def test_verify_presentation_flags_stripped_window():
    issues = verify_presentation({"tail": 0.0, "min_dur": 0.0}, None, [])
    types = {it["type"] for it in issues}
    assert "tail-stripped" in types
    assert "min-dur-stripped" in types


def test_verify_presentation_clean_with_defaults():
    issues = verify_presentation({"tail": 0.3, "min_dur": 1.0}, None, [])
    assert issues == []


def test_verify_presentation_first_cue_early():
    silences = [(0.0, 6.96)]
    issues = verify_presentation({"tail": 0.3, "min_dur": 1.0}, 5.0, silences)
    assert any(it["type"] == "first-cue-early" for it in issues)


# --------------------------- merge: isolated-silence hallucination ---------------------------

def test_in_silence_window():
    silences = [(0.0, 6.96)]
    assert _in_silence_window(0.0, 6.5, silences) is True
    assert _in_silence_window(7.0, 8.0, silences) is False


def test_drop_hallucination_isolated_silence():
    segs = [
        {"start": 0.0, "end": 6.5, "text": "Hubsan x4 H502E desire",
         "words": [{"start": 0.0, "end": 6.5}]},
        {"start": 6.54, "end": 9.08, "text": "If you can keep your head",
         "words": [{"start": 6.54, "end": 9.08}]},
    ]
    kept = drop_hallucination_segments(segs, silence_intervals=[(0.0, 6.96)])
    assert len(kept) == 1
    assert kept[0]["text"] == "If you can keep your head"


def test_drop_hallucination_keeps_real_speech():
    segs = [
        {"start": 6.54, "end": 9.08, "text": "If you can keep your head",
         "words": [{"start": 6.54, "end": 8.0}, {"start": 8.0, "end": 9.08}]},
    ]
    kept = drop_hallucination_segments(segs, silence_intervals=[(0.0, 6.0)])
    assert len(kept) == 1  # not inside the (0,6.0) silence


# --- V5 / ADR-020: fourth signal (audio-sharing tail-echo) ---


def test_drop_hallucination_audio_sharing_echo():
    """A tail-echo whose window is *contained* in the neighbor's window (it
    rides on the real audio) plus a zero-duration word must be dropped even
    though neither the collapse ratio nor the shared n-gram clears the original
    thresholds."""
    segs = [
        {"start": 53.38, "end": 54.72, "text": "give me a yogurt either way",
         "words": [{"start": 53.38, "end": 53.9}, {"start": 53.9, "end": 54.1},
                   {"start": 54.1, "end": 54.26}, {"start": 54.26, "end": 54.46},
                   {"start": 54.46, "end": 54.72}]},
        {"start": 54.22, "end": 54.72, "text": "I'm not hungry either way",
         "words": [{"start": 54.22, "end": 54.22}, {"start": 54.22, "end": 54.26},
                   {"start": 54.22, "end": 54.26}, {"start": 54.26, "end": 54.46},
                   {"start": 54.46, "end": 54.72}]},
    ]
    kept = drop_hallucination_segments(segs)
    assert len(kept) == 1
    assert kept[0]["text"] == "give me a yogurt either way"


def test_drop_hallucination_no_false_positive_on_adjacent_real_speech():
    """Two genuine adjacent cues with overlapping-but-distinct word intervals
    (e.g. overlapping talk) must NOT be dropped by the audio-sharing signal."""
    segs = [
        {"start": 1.0, "end": 3.0, "text": "hello there friend",
         "words": [{"start": 1.0, "end": 1.8}, {"start": 1.8, "end": 2.4},
                   {"start": 2.4, "end": 3.0}]},
        {"start": 3.1, "end": 5.0, "text": "how are you today",
         "words": [{"start": 3.1, "end": 3.7}, {"start": 3.7, "end": 4.3},
                   {"start": 4.3, "end": 5.0}]},
    ]
    assert drop_hallucination_segments(segs) == segs


# --- V5 / ADR-020: fifth signal (low Whisper acoustic confidence) ---


def test_drop_hallucination_low_confidence():
    """Whisper's own low avg_logprob (gated by no_speech_prob) drops a segment
    the dual-signal would have missed."""
    seg = {"start": 10.0, "end": 12.0, "text": "some repeated text here",
           "words": [{"start": 10.0, "end": 10.5}, {"start": 10.5, "end": 11.0},
                     {"start": 11.0, "end": 12.0}],
           "avg_logprob": -2.3, "no_speech_prob": 0.8}
    assert drop_hallucination_segments([seg]) == []


def test_drop_hallucination_keeps_high_confidence():
    """A real segment carrying good confidence fields is preserved."""
    seg = {"start": 1.0, "end": 3.0, "text": "real speech here",
           "words": [{"start": 1.0, "end": 1.5}, {"start": 1.5, "end": 2.5},
                     {"start": 2.5, "end": 3.0}],
           "avg_logprob": -0.35, "no_speech_prob": 0.04}
    assert drop_hallucination_segments([seg]) == [seg]


def test_low_confidence_signal_ignores_missing_fields():
    """Without confidence fields the fifth signal stays inert (no false drops)."""
    seg = {"start": 1.0, "end": 3.0, "text": "real speech here",
           "words": [{"start": 1.0, "end": 1.5}, {"start": 1.5, "end": 2.5},
                     {"start": 2.5, "end": 3.0}]}
    assert drop_hallucination_segments([seg]) == [seg]


def test_time_nested_echo_drop():
    """A phantom that re-emits the tail of the previous real cue, with its whole
    window *contained* in the neighbor's window + a zero-duration word, must drop
    (this is the exact 57s sitcom sample)."""
    segs = [
        {"start": 53.38, "end": 54.72, "text": "give me a yogurt either way",
         "words": [{"start": 53.38, "end": 53.9}, {"start": 53.9, "end": 54.1},
                   {"start": 54.1, "end": 54.26}, {"start": 54.26, "end": 54.46},
                   {"start": 54.46, "end": 54.72}]},
        {"start": 54.22, "end": 54.72, "text": "I'm not hungry either way",
         "words": [{"start": 54.22, "end": 54.22}, {"start": 54.22, "end": 54.26},
                   {"start": 54.22, "end": 54.26}, {"start": 54.26, "end": 54.46},
                   {"start": 54.46, "end": 54.72}]},
    ]
    kept = drop_hallucination_segments(segs)
    assert len(kept) == 1
    assert kept[0]["text"] == "give me a yogurt either way"


def test_boundary_blur_not_false_positive():
    """A genuine later cue whose first word interval merely blurs into the
    *next* neighbor's anchor (so it has a zero-duration word but does NOT nest
    inside any neighbor) must NOT be dropped — that is ordinary boundary blur,
    not an echo."""
    segs = [
        {"start": 135.8, "end": 136.74, "text": "It's a little early",
         "words": [{"start": 135.8, "end": 136.28}, {"start": 136.28, "end": 136.32},
                   {"start": 136.32, "end": 136.44}, {"start": 136.44, "end": 136.74}]},
        {"start": 136.92, "end": 137.24, "text": "I like it",
         "words": [{"start": 136.92, "end": 137.14}, {"start": 137.14, "end": 137.24},
                   {"start": 137.24, "end": 137.24}]},
        {"start": 137.06, "end": 137.82, "text": "like it with pizza",
         "words": [{"start": 137.06, "end": 137.28}, {"start": 137.28, "end": 137.36},
                   {"start": 137.36, "end": 137.46}, {"start": 137.46, "end": 137.82}]},
    ]
    assert drop_hallucination_segments(segs) == segs



# --------------------------- fill_gaps: genuine-silence hole guard ---------------------------

def test_hole_in_silence():
    silences = [(0.0, 6.96), (12.0, 14.0)]
    assert _hole_in_silence(0.0, 6.54, silences) is True
    assert _hole_in_silence(3.0, 4.0, silences) is True
    assert _hole_in_silence(7.0, 11.0, silences) is False


# --------------------------- audio_profile: per-chunk VAD routing (ADR-015) ---

def test_silence_fraction_clamps_to_duration():
    # 2s of silence inside a 10s window -> 0.2
    assert _silence_fraction([(0.0, 2.0)], 10.0) == 0.2
    # silence extends past the window -> clamped, capped at 1.0
    assert _silence_fraction([(0.0, 100.0)], 10.0) == 1.0
    # empty / zero duration -> 0
    assert _silence_fraction([], 10.0) == 0.0
    assert _silence_fraction([(0.0, 2.0)], 0.0) == 0.0


def test_route_vad_chunk_clean_anchors_to_silence():
    prof = AudioProfile(mean_vol=-16.0, max_vol=0.0,
                        silence_intervals=[(0.0, 2.0)], ok=True)
    assert route_vad_chunk(prof, 10.0) is True  # 0.2 >= 0.10


def test_route_vad_chunk_continuous_noise_bare():
    prof = AudioProfile(mean_vol=-16.0, max_vol=-3.0,
                        silence_intervals=[], ok=True)
    assert route_vad_chunk(prof, 10.0) is False  # no pauses -> bare


def test_route_vad_chunk_low_level_tuned_vad():
    # ADR-011 low-level branch: VAD on regardless of silence fraction
    prof = AudioProfile(mean_vol=-25.0, max_vol=-6.0,
                        silence_intervals=[], ok=True)
    assert route_vad_chunk(prof, 10.0) is True


def test_route_vad_chunk_unavailable_bare():
    assert route_vad_chunk(AudioProfile(ok=False), 10.0) is False


# --------------------------- verify: uncovered-audio lane (ADR-016 / T2b) -----

def test_find_uncovered_speech_flags_audio_between_cues():
    # 10s audio, cues cover [1,3] and [4,5]; [5,10] has audio but no cue
    segments = [
        {"start": 1.0, "end": 3.0},
        {"start": 4.0, "end": 5.0},
    ]
    uncovered = find_uncovered_speech(segments, [], 10.0, min_dur=2.0)
    # [0,1] is under min_dur; [3,4] under min_dur; [5,10] flagged
    assert uncovered == [(5.0, 10.0)]


def test_find_uncovered_speech_subtracts_silence():
    # [5,10] gap is actually silence -> NOT flagged
    segments = [{"start": 1.0, "end": 3.0}, {"start": 4.0, "end": 5.0}]
    uncovered = find_uncovered_speech(segments, [(5.0, 10.0)], 10.0, min_dur=2.0)
    assert uncovered == []


def test_find_uncovered_speech_carves_partial_silence():
    # [5,10] gap has silence [5,8] + real audio [8,10] -> flag [8,10]
    segments = [{"start": 1.0, "end": 3.0}, {"start": 4.0, "end": 5.0}]
    uncovered = find_uncovered_speech(segments, [(5.0, 8.0)], 10.0, min_dur=2.0)
    assert uncovered == [(8.0, 10.0)]


def test_find_uncovered_speech_full_coverage_clean():
    segments = [{"start": 0.0, "end": 5.0}, {"start": 5.0, "end": 10.0}]
    assert find_uncovered_speech(segments, [], 10.0, min_dur=2.0) == []


def test_find_uncovered_speech_head_and_tail():
    # leading [0,4] and trailing [8,10] are uncovered audio
    segments = [{"start": 4.0, "end": 8.0}]
    uncovered = find_uncovered_speech(segments, [], 10.0, min_dur=2.0)
    assert uncovered == [(0.0, 4.0), (8.0, 10.0)]


# --------------------------- verify: untranslated-latin lane (ADR-016/V14) ---

def test_find_untranslated_latin_words_flags_lowercase():
    assert find_untranslated_latin_words("也没那么大的 rivalry，所以放下吧！") == ["rivalry"]


def test_find_untranslated_latin_words_ignores_proper_nouns_and_acronyms():
    # 大写专名 (Ken/Barbenheimer) 与全大写 (OK/AI) 是合理保留，不 flag
    assert find_untranslated_latin_words("看 Ken 和 Katie，OK，Barbenheimer 之争") == []
    # 纯中文无残留
    assert find_untranslated_latin_words("也没那么大的竞争，所以放下吧！") == []


def test_find_untranslated_latin_words_multi():
    # 单字母 a 被有意排除（避免单字母噪声误报），多字母小写实词才 flag
    assert find_untranslated_latin_words("放下 rivalry，let it go 吧") == \
        ["rivalry", "let", "it", "go"]


# --------------------------- verify: semantic reread task (ADR-016/V14) ------

def test_build_semantic_reread_task_includes_zh_neighbors():
    segments = [
        {"text": "Barbenheimer rivalry behind us."},
        {"text": "that much of a rivalry, so just let it go!"},
        {"text": "Let it be."},
    ]
    zh = {0: "芭本海默之争抛在身后。", 1: "也没那么大的竞争，所以放下吧！", 2: "随它去。"}
    task = build_semantic_reread_task(segments, zh, window=2)
    pairs = {p["index"]: p for p in task["pairs"]}
    mid = pairs[1]
    assert mid["zh"] == "也没那么大的竞争，所以放下吧！"
    assert mid["context_before"] == ["Barbenheimer rivalry behind us."]
    assert mid["context_after"] == ["Let it be."]
    # 中文邻居也带上（判断"这句中文对不对"要看相邻中文是否连贯）
    assert mid["context_before_zh"] == ["芭本海默之争抛在身后。"]
    assert mid["context_after_zh"] == ["随它去。"]
