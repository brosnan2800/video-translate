"""TDD red tests for style-suffixed generate output (Spec 21 / ADR-027).

Covers:
- generate_subtitles(style="film") writes <base>.film.* four files
- no style -> filenames identical to current (golden backward compat)
- _resolve_out_base with style uses suffixed stem
- _prune_old_versions works on style-suffixed stems
"""
import json
import os

from video_translate.generate import (
    OUTPUT_SUFFIXES,
    _prune_old_versions,
    _resolve_out_base,
    generate_subtitles,
)


def _write_pair(tmp_path, base):
    segs = [{"start": 0.0, "end": 1.0, "text": "hi"}]
    zh = {"0": "嗨"}
    seg_p = tmp_path / f"{base}.segments_en.json"
    zh_p = tmp_path / f"{base}.zh_segments.json"
    seg_p.write_text(json.dumps(segs))
    zh_p.write_text(json.dumps(zh))
    return str(seg_p), str(zh_p)


def test_generate_style_suffix_writes_four_files(tmp_path):
    seg_p, zh_p = _write_pair(tmp_path, "clip")
    written = generate_subtitles(seg_p, zh_p, str(tmp_path), base="clip", style="film")
    expected = os.path.join(str(tmp_path), "clip", "clip.film.bilingual.srt")
    assert expected in written
    assert os.path.exists(expected)
    for suffix in OUTPUT_SUFFIXES:
        assert os.path.exists(os.path.join(str(tmp_path), "clip", "clip.film" + suffix))


def test_generate_no_style_keeps_legacy_names(tmp_path):
    seg_p, zh_p = _write_pair(tmp_path, "clip")
    written = generate_subtitles(seg_p, zh_p, str(tmp_path), base="clip")
    expected = os.path.join(str(tmp_path), "clip", "clip.bilingual.srt")
    assert expected in written
    assert not os.path.exists(os.path.join(str(tmp_path), "clip", "clip.film.bilingual.srt"))


def test_resolve_out_base_with_style(tmp_path):
    d, b = _resolve_out_base(str(tmp_path), "clip", flat=False, style="literal")
    assert d == os.path.join(str(tmp_path), "clip")
    assert b == "clip.literal"


def test_prune_on_style_suffix(tmp_path):
    sub = os.path.join(str(tmp_path), "clip")
    os.makedirs(sub, exist_ok=True)
    for stem, mtime in (("clip.literal", 1000), ("clip.literal_v1", 2000),
                        ("clip.literal_v2", 3000)):
        for suffix in OUTPUT_SUFFIXES:
            with open(os.path.join(sub, stem + suffix), "w") as f:
                f.write(stem + suffix)
            os.utime(os.path.join(sub, stem + suffix), (mtime, mtime))
    _prune_old_versions(sub, "clip.literal")
    remaining = sorted(fn for fn in os.listdir(sub) if fn.endswith(".bilingual.srt"))
    assert remaining == ["clip.literal_v1.bilingual.srt", "clip.literal_v2.bilingual.srt"]
