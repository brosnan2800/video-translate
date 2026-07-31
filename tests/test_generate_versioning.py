"""Versioning / per-video-subfolder behavior of generate (V5).

Final subtitle outputs go into `<outdir>/<base>/` with a collision-based
version suffix; `--flat` keeps the legacy behavior. `_prune_old_versions`
retains only the two most-recent sets.
"""
import json
import os

from video_translate.generate import (
    OUTPUT_SUFFIXES,
    _prune_old_versions,
    _resolve_out_base,
    generate_subtitles,
)


def _write_set(out_dir: str, stem: str, mtime: float | None = None) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for suffix in OUTPUT_SUFFIXES:
        p = os.path.join(out_dir, stem + suffix)
        with open(p, "w") as f:
            f.write(f"{stem}{suffix}\n")
    if mtime is not None:
        for suffix in OUTPUT_SUFFIXES:
            os.utime(os.path.join(out_dir, stem + suffix), (mtime, mtime))


def test_resolve_out_base_flat_returns_outdir(tmp_path):
    d, b = _resolve_out_base(str(tmp_path), "clip", flat=True)
    assert (d, b) == (str(tmp_path), "clip")


def test_resolve_out_base_first_run_no_suffix(tmp_path):
    d, b = _resolve_out_base(str(tmp_path), "clip", flat=False)
    assert d == os.path.join(str(tmp_path), "clip")
    assert b == "clip"


def test_resolve_out_base_collision_bumps(tmp_path):
    sub = os.path.join(str(tmp_path), "clip")
    _write_set(sub, "clip")  # a previous run's output already present
    d, b = _resolve_out_base(str(tmp_path), "clip", flat=False)
    assert b == "clip_v1"


def test_resolve_out_base_bump_from_v1_to_v2(tmp_path):
    sub = os.path.join(str(tmp_path), "clip")
    _write_set(sub, "clip")
    _write_set(sub, "clip_v1")
    d, b = _resolve_out_base(str(tmp_path), "clip", flat=False)
    assert b == "clip_v2"


def test_prune_keeps_two_newest(tmp_path):
    sub = os.path.join(str(tmp_path), "clip")
    _write_set(sub, "clip", mtime=1000)
    _write_set(sub, "clip_v1", mtime=2000)
    _write_set(sub, "clip_v2", mtime=3000)
    _prune_old_versions(sub, "clip")
    remaining = sorted(fn for fn in os.listdir(sub) if fn.endswith(".bilingual.srt"))
    assert remaining == ["clip_v1.bilingual.srt", "clip_v2.bilingual.srt"]


def test_prune_noop_when_two_or_fewer(tmp_path):
    sub = os.path.join(str(tmp_path), "clip")
    _write_set(sub, "clip", mtime=1000)
    _write_set(sub, "clip_v1", mtime=2000)
    _prune_old_versions(sub, "clip")
    # nothing removed: both sets survive
    assert sorted(os.listdir(sub)) == sorted(
        s for stem in ("clip", "clip_v1") for s in (stem + suf for suf in OUTPUT_SUFFIXES)
    )


def test_generate_subtitles_writes_into_subfolder(tmp_path):
    segs = [{"start": 0.0, "end": 1.0, "text": "hi"}]
    zh = {"0": "嗨"}
    seg_p = tmp_path / "clip.segments_en.json"
    zh_p = tmp_path / "clip.zh_segments.json"
    seg_p.write_text(json.dumps(segs))
    zh_p.write_text(json.dumps(zh))
    written = generate_subtitles(str(seg_p), str(zh_p), str(tmp_path), base="clip")
    expected = os.path.join(str(tmp_path), "clip", "clip.bilingual.srt")
    assert expected in written
    assert os.path.exists(expected)


def test_generate_subtitles_flat_legacy(tmp_path):
    segs = [{"start": 0.0, "end": 1.0, "text": "hi"}]
    zh = {"0": "嗨"}
    seg_p = tmp_path / "clip.segments_en.json"
    zh_p = tmp_path / "clip.zh_segments.json"
    seg_p.write_text(json.dumps(segs))
    zh_p.write_text(json.dumps(zh))
    written = generate_subtitles(
        str(seg_p), str(zh_p), str(tmp_path), base="clip", flat=True
    )
    expected = os.path.join(str(tmp_path), "clip.bilingual.srt")
    assert expected in written
    assert not os.path.exists(os.path.join(str(tmp_path), "clip"))  # no subfolder
