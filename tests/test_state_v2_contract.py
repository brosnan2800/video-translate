"""ADR-035 M2 — state schema v2 迁移 + 声学事实 / audio_source 落盘单测。"""
from __future__ import annotations

import json

import pytest

from video_translate import state as vt_state
from video_translate.audio_profile import AudioProfile


def _write_state(tmp_path, base, blob) -> str:
    p = tmp_path / f"{base}.vt_state.json"
    p.write_text(json.dumps(blob), encoding="utf-8")
    return str(tmp_path)


# --------------------------------------------------------------------------- #
# schema 迁移
# --------------------------------------------------------------------------- #

def test_v1_state_migrates_preserving_content(tmp_path):
    blob = {
        "schema_version": 1,
        "base": "clip",
        "decisions": {"routing": {"value": {"vad": False}}},
        "stages": {"transcribe": {"status": "ok"}},
    }
    outdir = _write_state(tmp_path, "clip", blob)
    st = vt_state.load(outdir, "clip")
    assert st["schema_version"] == vt_state.SCHEMA_VERSION == 2
    # ADR-031 D6：迁移不丢决策与阶段状态
    assert st["decisions"]["routing"]["value"]["vad"] is False
    assert st["stages"]["transcribe"]["status"] == "ok"


def test_future_schema_treated_as_missing(tmp_path):
    blob = {"schema_version": 999, "base": "clip"}
    outdir = _write_state(tmp_path, "clip", blob)
    assert vt_state.load(outdir, "clip") == {}


def test_v2_state_loads_unchanged(tmp_path):
    blob = {"schema_version": 2, "base": "clip", "audio_profile": {"duration": 5.0}}
    outdir = _write_state(tmp_path, "clip", blob)
    st = vt_state.load(outdir, "clip")
    assert st["audio_profile"]["duration"] == 5.0


# --------------------------------------------------------------------------- #
# 声学事实数据段（artifact "audio_profile"，单一生产 = preflight）
# --------------------------------------------------------------------------- #

def test_record_and_get_acoustics_roundtrip(tmp_path):
    outdir = str(tmp_path)
    prof = AudioProfile(mean_vol=-24.0, max_vol=-6.0,
                        silence_intervals=[(0.0, 1.0), (2.0, 2.5)],
                        duration=10.0, ok=True)
    data = vt_state.record_acoustics(outdir, "clip", prof)
    assert data["duration"] == 10.0
    assert data["silence_fraction"] == pytest.approx(0.15)
    # 探测参数是 artifact 身份的一部分（verify 复用前要核对）
    assert data["noise"] == "-30dB"
    assert data["d"] == 0.3
    assert vt_state.get_acoustics(outdir, "clip") is not None
    # JSON 往返把 tuple 归一成 list——消费者按 [tuple(iv) ...] 还原（cli 已如此）。
    got = vt_state.get_acoustics(outdir, "clip")
    got["silence_intervals"] = [tuple(iv) for iv in got["silence_intervals"]]
    assert got == data


def test_record_acoustics_rejects_failed_probe(tmp_path):
    outdir = str(tmp_path)
    assert vt_state.record_acoustics(outdir, "clip", AudioProfile(ok=False)) is None
    assert vt_state.record_acoustics(outdir, "clip", None) is None
    assert vt_state.get_acoustics(outdir, "clip") is None


def test_record_acoustics_persists_across_loads(tmp_path):
    outdir = str(tmp_path)
    prof = AudioProfile(mean_vol=-10.0, max_vol=-1.0, silence_intervals=[],
                        duration=3.0, ok=True)
    vt_state.record_acoustics(outdir, "clip", prof)
    # 空 silence_intervals 是"探测成功、全片无静音"的真实结果，不是缺失
    got = vt_state.get_acoustics(outdir, "clip")
    assert got is not None
    assert got["silence_intervals"] == []


# --------------------------------------------------------------------------- #
# audio_source（vocals.wav 显式记录，不再靠 fingerprint 反推）
# --------------------------------------------------------------------------- #

def test_record_and_get_audio_source_roundtrip(tmp_path):
    outdir = str(tmp_path)
    vt_state.record_audio_source(
        outdir, "clip", vocals_wav="videos/clip.ab12.vocals.wav",
        vsep_backend="demucs", vsep_model="htdemucs", vsep_input_hash="abcd1234")
    got = vt_state.get_audio_source(outdir, "clip")
    assert got == {
        "vocals_wav": "videos/clip.ab12.vocals.wav",
        "vsep_backend": "demucs",
        "vsep_model": "htdemucs",
        "vsep_input_hash": "abcd1234",
    }


def test_get_audio_source_missing_is_none(tmp_path):
    assert vt_state.get_audio_source(str(tmp_path), "clip") is None
