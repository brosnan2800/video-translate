"""Regression tests for issue #001: real inter-scene silence swallowed by the
ASR, then "fixed" in V3 by splitting cues at word-level silence (Spec 15 / ADR-009).

The fast tests below prove the *mechanism* using synthetic word timelines — no
model, no ffmpeg. The ``@slow`` test documents the end-to-end expectation against
a real video (skipped unless the source + model are present).
"""
import os

import pytest

from video_translate.merge import _split_by_gap, split_long_cues

# A cue that the ASR emitted as ONE segment even though two short scenes are
# separated by ~2.3s of real silence (issue #001 symptom: cue appears with no gap).
_WORDS = [
    {"word": "Scene", "start": 0.0, "end": 0.35},
    {"word": "one", "start": 0.4, "end": 0.7},
    {"word": "ends.", "start": 0.75, "end": 1.0},
    {"word": "Scene", "start": 3.3, "end": 3.6},   # 2.3s silence before this word
    {"word": "two", "start": 3.7, "end": 4.0},
    {"word": "begins.", "start": 4.1, "end": 4.4},
]


def test_real_silence_is_preserved_as_two_cues():
    """The swallowed silence must reappear as a split between two cues."""
    seg = {
        "start": _WORDS[0]["start"],
        "end": _WORDS[-1]["end"],
        "text": " ".join(w["word"] for w in _WORDS),
        "words": list(_WORDS),
    }
    out = split_long_cues([seg], max_gap=1.0)
    assert len(out) == 2, "real silence must split the cue in two"
    # the silence survives between the two cues
    gap = out[1]["start"] - out[0]["end"]
    assert gap > 1.0, "expected the real silence to remain between cues"
    # no word is lost and order is preserved
    flat = [w for s in out for w in s["words"]]
    assert flat == _WORDS


def test_split_by_gap_is_word_boundary_only():
    """Split happens strictly at word boundaries — never inside a word."""
    groups = _split_by_gap(_WORDS, max_gap=1.0)
    assert len(groups) == 2
    assert [w["word"] for w in groups[0]] == ["Scene", "one", "ends."]
    assert [w["word"] for w in groups[1]] == ["Scene", "two", "begins."]


def test_no_false_split_when_silence_is_small():
    """Adjacent fragments with only tiny gaps must NOT be split apart."""
    words = [
        {"word": "a", "start": 0.0, "end": 0.3},
        {"word": "b", "start": 0.4, "end": 0.7},   # 0.1s gap
        {"word": "c", "start": 0.8, "end": 1.1},   # 0.1s gap
    ]
    groups = _split_by_gap(words, max_gap=1.0)
    assert len(groups) == 1


@pytest.mark.slow
def test_end_to_end_real_silence(tmp_path):
    """End-to-end expectation for issue #001 against the source video.

    Skipped unless the source video + large-v3 model are present. Documents that a
    real scene-break silence should NOT be collapsed into a single zero-gap cue.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    video = None
    for name in ("steveharvy-the apollo story.mp4",):
        for d in (os.path.join(repo_root, "videos"), os.path.dirname(repo_root)):
            p = os.path.join(d, name)
            if os.path.exists(p):
                video = p
    if video is None:
        pytest.skip("source video not found")
    pytest.importorskip("faster_whisper")
    from video_translate.cli import EXIT_AWAITING_AGENT, main
    rc = main(["run", video, "--outdir", str(tmp_path), "--engine", "agent",
               "--no-proxy"])
    assert rc == EXIT_AWAITING_AGENT
    segs = __import__("json").load(
        open(os.path.join(str(tmp_path), "apollo_story.segments_en.json")))
    # every adjacent pair must show a real gap (no zero-gap collapse) somewhere,
    # i.e. the ASR did not merge two scenes into one cue silently.
    assert any(
        segs[i + 1]["start"] - segs[i]["end"] > 0.3
        for i in range(len(segs) - 1)
    )
