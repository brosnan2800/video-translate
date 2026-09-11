"""Tests for the control-plane state file (<base>.vt_state.json, §2.1)."""
import json
import os

import pytest

from video_translate.state import (
    STAGE_ORDER,
    current_stage,
    ensure_state,
    infer_stage,
    load,
    new_state,
    rebuild_state,
    record_decision,
    record_stage,
    save,
    segment_sha,
    set_stage,
    stage_status,
    state_needs_rebuild,
    state_path,
)


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return str(path)


def test_state_path_naming(tmp_path):
    p = state_path(tmp_path, "apollo_story")
    assert p == tmp_path / "apollo_story" / "apollo_story.vt_state.json"


def test_save_load_roundtrip(tmp_path):
    st = new_state("clip", video="videos/clip.mp4")
    set_stage(st, "translate")
    record_decision(st, "align", "auto", origin="default", resolved="whisperx")
    record_decision(st, "separate_vocals", True, origin="explicit")
    record_stage(st, "transcribe", segments_sha="sha256:abc", n_segments=42)

    save(tmp_path, "clip", st)
    loaded = load(tmp_path, "clip")
    assert loaded["stage"] == "translate"
    assert loaded["decisions"]["align"]["origin"] == "default"
    assert loaded["decisions"]["align"]["resolved"] == "whisperx"
    assert loaded["decisions"]["separate_vocals"]["origin"] == "explicit"
    assert loaded["stages"]["transcribe"]["n_segments"] == 42
    assert loaded["schema_version"] == 2


def test_load_missing_returns_empty(tmp_path):
    assert load(tmp_path, "nope") == {}


def test_load_wrong_schema_returns_empty(tmp_path):
    p = state_path(tmp_path, "clip")
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_json(p, {"schema_version": 999, "stage": "x"})
    assert load(tmp_path, "clip") == {}


def test_load_corrupt_json_returns_empty(tmp_path):
    p = state_path(tmp_path, "clip")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json !", encoding="utf-8")
    assert load(tmp_path, "clip") == {}


def test_segment_sha_is_stable_and_content_anchored(tmp_path):
    f = tmp_path / "seg.json"
    f.write_text("[1,2,3]", encoding="utf-8")
    s1 = segment_sha(f)
    f.write_text("[1,2,4]", encoding="utf-8")
    s2 = segment_sha(f)
    assert s1.startswith("sha256:")
    assert s1 != s2
    assert segment_sha(f) == s2  # deterministic


def test_set_stage_validates_order():
    st = new_state("a")
    assert current_stage(st) == "preflight"
    set_stage(st, "generate")
    assert current_stage(st) == "generate"
    with pytest.raises(ValueError):
        set_stage(st, "not-a-stage")
    assert STAGE_ORDER == ("preflight", "transcribe", "translate", "generate", "verify")


def test_stage_status_default_pending():
    assert stage_status(new_state("a"), "transcribe") == {"status": "pending"}


def test_infer_stage_from_artifacts(tmp_path):
    wd = tmp_path / "clip"
    wd.mkdir(parents=True, exist_ok=True)
    assert infer_stage(tmp_path, "clip") == "preflight"
    _write_json(wd / "clip.segments_en.json", [])
    assert infer_stage(tmp_path, "clip") == "translate"
    _write_json(wd / "clip.zh_segments.json", {"0": "你好"})
    assert infer_stage(tmp_path, "clip") == "generate"
    (wd / "clip.bilingual.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n",
                                           encoding="utf-8")
    assert infer_stage(tmp_path, "clip") == "verify"


def test_rebuild_state_from_artifacts(tmp_path):
    wd = tmp_path / "clip"
    wd.mkdir(parents=True, exist_ok=True)
    seg = wd / "clip.segments_en.json"
    seg.write_text('[{"start": 0.0, "end": 1.0}]', encoding="utf-8")
    sha = segment_sha(seg)
    st = rebuild_state(tmp_path, "clip")
    assert st["stage"] == "translate"
    assert st["stages"]["transcribe"]["segments_sha"] == sha


def test_ensure_state_self_heals_missing(tmp_path):
    wd = tmp_path / "clip"
    wd.mkdir(parents=True, exist_ok=True)
    seg = wd / "clip.segments_en.json"
    seg.write_text("[]", encoding="utf-8")
    st = ensure_state(tmp_path, "clip")
    assert state_path(tmp_path, "clip").is_file()
    assert st["stages"]["transcribe"]["segments_sha"] == segment_sha(seg)
    # Second call does not rebuild (state now valid).
    assert ensure_state(tmp_path, "clip")["stage"] == "translate"


def test_state_needs_rebuild_when_segments_changed(tmp_path):
    wd = tmp_path / "clip"
    wd.mkdir(parents=True, exist_ok=True)
    seg = wd / "clip.segments_en.json"
    seg.write_text("[old]", encoding="utf-8")
    st = new_state("clip")
    record_stage(st, "transcribe", segments_sha=segment_sha(seg))
    save(tmp_path, "clip", st)
    assert state_needs_rebuild(tmp_path, "clip") is False
    seg.write_text("[new content]", encoding="utf-8")
    assert state_needs_rebuild(tmp_path, "clip") is True


def test_per_base_isolation(tmp_path):
    save(tmp_path, "a", new_state("a"))
    save(tmp_path, "b", new_state("b"))
    assert (tmp_path / "a" / "a.vt_state.json").is_file()
    assert (tmp_path / "b" / "b.vt_state.json").is_file()