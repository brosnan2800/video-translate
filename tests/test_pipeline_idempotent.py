"""Idempotency / determinism of the pure stages (Spec 07 invariants)."""
import json
import os

from video_translate.generate import build_outputs, generate_subtitles


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_generate_is_deterministic(tmp_path, golden_segments_path, golden_zh_path):
    """Running generate twice yields identical bytes (no timestamps recomputed)."""
    d1 = tmp_path / "run1"
    d2 = tmp_path / "run2"
    for d in (d1, d2):
        generate_subtitles(golden_segments_path, golden_zh_path, str(d),
                           base="apollo_story", flat=True, progress=lambda *_: None)
    for suffix in (".bilingual.srt", ".zh.srt", ".en.srt", ".txt"):
        a = open(os.path.join(d1, "apollo_story" + suffix), "rb").read()
        b = open(os.path.join(d2, "apollo_story" + suffix), "rb").read()
        assert a == b


def test_timestamps_not_recomputed(golden_segments_path, golden_zh_path):
    """The design invariant: generate copies segment start/end verbatim."""
    segments = _load(golden_segments_path)
    zh = {int(k): v for k, v in _load(golden_zh_path).items()}
    out = build_outputs(segments, zh)
    # first cue window in bilingual must reflect segment[0]'s exact times
    from video_translate.srt_utils import srt_time
    s0 = segments[0]
    expected_line = f"{srt_time(s0['start'])} --> {srt_time(s0['end'])}"
    assert expected_line in out[".bilingual.srt"]
