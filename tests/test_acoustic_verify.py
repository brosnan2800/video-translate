"""Tests for the ADR-012 acoustic-reference features (Spec 18).

Covers: audio_profile parsing + VAD routing, verify lanes (pure functions),
and the two silence-based hallucination guards (merge + fill_gaps). All pure —
no ffmpeg / model required.
"""
from video_translate.audio_profile import (
    AudioProfile, parse_silencedetect, parse_volumedetect, recommend_vad,
)
from video_translate.verify import (
    CROSS_SILENCE, FIRST_CUE_EARLY, IN_SILENCE, cue_bounds, cue_cross_silence,
    cue_in_silence, first_cue_early, verify_acoustic, verify_presentation,
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


# --------------------------- fill_gaps: genuine-silence hole guard ---------------------------

def test_hole_in_silence():
    silences = [(0.0, 6.96), (12.0, 14.0)]
    assert _hole_in_silence(0.0, 6.54, silences) is True
    assert _hole_in_silence(3.0, 4.0, silences) is True
    assert _hole_in_silence(7.0, 11.0, silences) is False
