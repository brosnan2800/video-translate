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

    outputs = build_outputs(segments, zh)
    for suffix in (".bilingual.srt", ".zh.srt", ".en.srt", ".txt"):
        golden_path = os.path.join(golden_dir, "apollo_story" + suffix)
        with open(golden_path, encoding="utf-8") as f:
            expected = f.read()
        assert outputs[suffix] == expected, f"drift in {suffix}"


def test_generate_writes_four_files_byte_exact(tmp_path, golden_dir,
                                               golden_segments_path, golden_zh_path):
    written = generate_subtitles(golden_segments_path, golden_zh_path, str(tmp_path),
                                 base="apollo_story", progress=lambda *_: None)
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
