"""Golden regression for the generate stage (Spec 04).

THE load-bearing test: feeding the golden segments+zh must reproduce all four
golden output files byte-for-byte. If this fails, subtitle output has drifted.
"""
import json
import os

from video_translate.generate import build_outputs, generate_subtitles


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_build_outputs_byte_exact(golden_dir, golden_segments_path, golden_zh_path):
    segments = _load(golden_segments_path)
    zh_raw = _load(golden_zh_path)
    zh = {int(k): v for k, v in zh_raw.items()}

    outputs = build_outputs(segments, zh, gap=0.2)
    for suffix in (".bilingual.srt", ".zh.srt", ".en.srt", ".txt"):
        golden_path = os.path.join(golden_dir, "apollo_story" + suffix)
        with open(golden_path, encoding="utf-8") as f:
            expected = f.read()
        assert outputs[suffix] == expected, f"drift in {suffix}"


def test_generate_writes_four_files_byte_exact(tmp_path, golden_dir,
                                               golden_segments_path, golden_zh_path):
    written = generate_subtitles(golden_segments_path, golden_zh_path, str(tmp_path),
                                 base="apollo_story", gap=0.2, flat=True,
                                 progress=lambda *_: None)
    assert len(written) == 4
    for suffix in (".bilingual.srt", ".zh.srt", ".en.srt", ".txt"):
        got = open(os.path.join(tmp_path, "apollo_story" + suffix), "rb").read()
        exp = open(os.path.join(golden_dir, "apollo_story" + suffix), "rb").read()
        assert got == exp, f"byte drift in {suffix}"


def test_bilingual_keeps_english_when_zh_missing():
    segments = [{"start": 0, "end": 1, "text": "hello"}]
    out = build_outputs(segments, {})  # no zh
    assert "hello" in out[".bilingual.srt"]
    # zh-only file has no cue
    assert out[".zh.srt"].strip() == ""


def test_v1_golden_preserved(golden_dir):
    """V1 baseline archived as .v1 must exist (Stage 4 archive)."""
    for suffix in (".segments_en.json", ".zh_segments.json",
                   ".bilingual.srt", ".zh.srt", ".en.srt", ".txt"):
        p = os.path.join(golden_dir, "apollo_story.v1" + suffix)
        assert os.path.exists(p), f"V1 golden archive missing: {p}"


# --- V3: word-level boundaries + --gap clamp (pure, synthetic) ---


def test_build_outputs_uses_word_boundaries():
    """When words are present, the cue window uses first-word start / last-word
    end instead of the (silence-padded) segment level."""
    segments = [{
        "start": 0.0, "end": 2.0, "text": "Hi there",
        "words": [{"word": "Hi", "start": 0.3, "end": 0.6},
                  {"word": "there", "start": 0.7, "end": 1.5}],
    }]
    out = build_outputs(segments, {0: "你好"}, gap=0.0)
    # first cue window starts at 0.3, ends at 1.5 (word-level, not 0.0/2.0)
    assert "00:00:00,300" in out[".bilingual.srt"]
    assert "00:00:01,500" in out[".bilingual.srt"]
    assert "00:00:00,000" not in out[".bilingual.srt"].split("\n\n")[0]


def test_build_outputs_fallback_no_words():
    """Without words, the segment-level window is used (V2-compatible path)."""
    segments = [{"start": 1.0, "end": 3.0, "text": "hello"}]
    out = build_outputs(segments, {0: "你好"}, gap=0.0)
    assert "00:00:01,000" in out[".bilingual.srt"]
    assert "00:00:03,000" in out[".bilingual.srt"]


def test_build_outputs_gap_clamp_no_overlap():
    """--gap never fabricates silence: it only trims trailing silence so adjacent
    cues keep `gap` spacing and never overlap."""
    segments = [
        {"start": 0.0, "end": 4.0, "text": "a",
         # word end 3.05 > next.start(3.1) - gap(0.2) = 2.9 -> trimmed to 2.9
         "words": [{"word": "a", "start": 0.1, "end": 3.05}]},
        {"start": 3.1, "end": 4.0, "text": "b",
         "words": [{"word": "b", "start": 3.1, "end": 3.9}]},
    ]
    out = build_outputs(segments, {0: "甲", 1: "乙"}, gap=0.2)
    # cue A end trimmed to next.start - gap = 3.1 - 0.2 = 2.9
    assert "00:00:02,900" in out[".bilingual.srt"]
    # cue B still starts at its real word boundary 3.1
    assert "00:00:03,100" in out[".bilingual.srt"]


def test_build_outputs_gap_does_not_invent_when_real_gap_larger():
    """If the real gap already exceeds `gap`, --gap does nothing."""
    segments = [
        {"start": 0.0, "end": 1.0, "text": "a",
         "words": [{"word": "a", "start": 0.1, "end": 0.9}]},
        {"start": 5.0, "end": 6.0, "text": "b",
         "words": [{"word": "b", "start": 5.1, "end": 5.9}]},
    ]
    out = build_outputs(segments, {0: "甲", 1: "乙"}, gap=0.2)
    # cue A keeps its real end 0.9 (no clamp needed; next.start - gap = 4.8 > 0.9)
    assert "00:00:00,900" in out[".bilingual.srt"]
    # cue B still starts at real 5.1
    assert "00:00:05,100" in out[".bilingual.srt"]


# --- V4: min-dur display extension ---


def test_min_dur_extends_short_cue_display_window():
    """A 0.2s orphan cue ('coding') gets its DISPLAY end extended to min_dur;
    the start (alignment fact) never moves."""
    segments = [
        {"start": 56.47, "end": 56.67, "text": "coding",
         "words": [{"word": " coding", "start": 56.47, "end": 56.67}]},
        {"start": 60.0, "end": 61.0, "text": "later",
         "words": [{"word": " later", "start": 60.0, "end": 61.0}]},
    ]
    out = build_outputs(segments, {0: "编码", 1: "稍后"}, gap=0.2, min_dur=1.0)
    # extended to 56.47 + 1.0 = 57.47; plenty of room before next.start-gap=59.8
    assert "00:00:56,470 --> 00:00:57,470" in out[".bilingual.srt"]
    # next cue untouched
    assert "00:01:00,000 --> 00:01:01,000" in out[".bilingual.srt"]


def test_min_dur_yields_to_gap_clamp_when_room_insufficient():
    """min_dur is the soft target; non-overlap (gap clamp) is the hard
    constraint and wins when there is not enough room."""
    segments = [
        {"start": 10.0, "end": 10.2, "text": "uh",
         "words": [{"word": " uh", "start": 10.0, "end": 10.2}]},
        {"start": 10.5, "end": 11.5, "text": "next",
         "words": [{"word": " next", "start": 10.5, "end": 11.5}]},
    ]
    out = build_outputs(segments, {0: "呃", 1: "下一句"}, gap=0.2, min_dur=1.0)
    # wants 11.0 but clamped to next.start - gap = 10.5 - 0.2 = 10.3
    assert "00:00:10,000 --> 00:00:10,300" in out[".bilingual.srt"]
    # cue B start never moved
    assert "00:00:10,500" in out[".bilingual.srt"]


def test_min_dur_default_off_keeps_short_cue():
    """Library default min_dur=0.0: short cues pass through (golden compat)."""
    segments = [{"start": 10.0, "end": 10.2, "text": "uh",
                 "words": [{"word": " uh", "start": 10.0, "end": 10.2}]}]
    out = build_outputs(segments, {0: "呃"}, gap=0.2)
    assert "00:00:10,000 --> 00:00:10,200" in out[".bilingual.srt"]


# --- V6 (B1'): display offset + tail extension ---


def _one(start, end, text=" hi"):
    return {"start": start, "end": end, "text": text.strip(),
            "words": [{"word": text, "start": start, "end": end}]}


def test_offset_shifts_whole_display_window_later():
    """Positive offset moves BOTH start and end later (drift correction)."""
    out = build_outputs([_one(10.0, 11.0)], {0: "你好"}, offset=0.25)
    assert "00:00:10,250 --> 00:00:11,250" in out[".bilingual.srt"]


def test_offset_negative_clamped_at_zero():
    """A negative offset must never produce a negative timestamp."""
    out = build_outputs([_one(0.1, 1.0)], {0: "你好"}, offset=-0.5)
    assert out[".bilingual.srt"].startswith("1\n00:00:00,000 --> 00:00:00,500")


def test_tail_extends_end_only():
    out = build_outputs([_one(10.0, 11.0)], {0: "你好"}, tail=0.3)
    assert "00:00:10,000 --> 00:00:11,300" in out[".bilingual.srt"]


def test_tail_yields_to_gap_clamp():
    """tail must never eat into the next cue: the gap clamp wins."""
    segments = [_one(10.0, 11.0, " a"), _one(11.1, 12.0, " b")]
    out = build_outputs(segments, {0: "甲", 1: "乙"}, gap=0.2, tail=0.5)
    # wants 11.5, clamped to next.start - gap = 11.1 - 0.2 = 10.9
    assert "00:00:10,000 --> 00:00:10,900" in out[".bilingual.srt"]


def test_tail_clamp_runs_even_when_gap_is_zero():
    """With gap=0 the clamp pass still runs (tail activated it), so cues touch
    but never overlap."""
    segments = [_one(10.0, 11.0, " a"), _one(11.1, 12.0, " b")]
    out = build_outputs(segments, {0: "甲", 1: "乙"}, gap=0.0, tail=0.5)
    assert "00:00:10,000 --> 00:00:11,100" in out[".bilingual.srt"]


def test_offset_and_tail_default_off():
    """Library defaults keep the historical window (golden compat)."""
    out = build_outputs([_one(10.0, 11.0)], {0: "你好"}, gap=0.2)
    assert "00:00:10,000 --> 00:00:11,000" in out[".bilingual.srt"]
