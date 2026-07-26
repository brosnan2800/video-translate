"""Golden regression for the merge stage (Spec 08, ADR-004)."""
import json

from video_translate.merge import merge_segments, split_long_cues


def test_merge_produces_byte_exact_golden(golden_raw_segments_path,
                                          golden_merged_segments_path):
    raw = json.load(open(golden_raw_segments_path, encoding="utf-8"))
    merged = merge_segments(raw)
    actual = json.dumps(merged, ensure_ascii=False, indent=0)
    expected = open(golden_merged_segments_path, encoding="utf-8").read()
    assert actual == expected


def test_merge_idempotent():
    """Merging an already-merged list is stable (no further changes)."""
    segs = [{"start": 0, "end": 1, "text": "a"},
            {"start": 1.1, "end": 2, "text": "b."}]
    once = merge_segments(segs, max_gap=1.0)
    twice = merge_segments(once, max_gap=1.0)
    assert once == twice


def test_merge_does_not_increase_count(golden_raw_segments_path,
                                       golden_merged_segments_path):
    raw = json.load(open(golden_raw_segments_path, encoding="utf-8"))
    merged = json.load(open(golden_merged_segments_path, encoding="utf-8"))
    assert len(merged) <= len(raw)
    assert len(merged) < len(raw)  # apollo has real mid-sentence fragments


def test_split_golden_noop_without_words(golden_merged_segments_path):
    """The V2 golden has no word timestamps; split_long_cues must leave it
    unchanged (degenerate / fallback path — no split without words)."""
    merged = json.load(open(golden_merged_segments_path, encoding="utf-8"))
    out = split_long_cues(merged, max_chars=42)
    assert out == merged
