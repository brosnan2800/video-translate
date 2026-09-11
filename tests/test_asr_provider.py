"""Spec 25 / ADR-038 — ASRProvider 接口契约测试（第一步）。

验收目标：接口层新增，**现有行为零变化**（第一步不接入任何调用路径）。
"""
from __future__ import annotations

import inspect
import json

from video_translate import capabilities
from video_translate.artifacts import validate_artifact
from video_translate.asr import (
    ASRProvider,
    FasterWhisperProvider,
    TranscribeResult,
    TranscriberConfig,
)

# config 字段 -> transcribe_video 形参名（命名单一来源对照表）
_FIELD_TO_PARAM = {
    "model": "model_name",
    "chunk": "chunk",
    "threads": "threads",
    "lang": "lang",
    "vad_threshold": "vad_threshold",
    "use_vad": "use_vad",
    "no_speech_threshold": "no_speech_threshold",
    "temperature": "temperature",
    "device": "device",
    "compute_type": "compute_type",
    "adaptive_vad": "adaptive_vad",
    "audio_source": "audio_source",
    "separate_vocals": "separate_vocals",
    "vocal_sep_backend": "vocal_sep_backend",
    "vocal_sep_model": "vocal_sep_model",
    "vocal_sep_input_hash": "vocal_sep_input_hash",
    "align_backend": "align_backend",
    "align_allow_degrade": "align_allow_degrade",
}


def _tv_params() -> dict:
    from video_translate.transcribe import transcribe_video

    return dict(inspect.signature(transcribe_video).parameters)


# --------------------------- 默认值等价（防漂移） ---------------------------

def test_config_defaults_match_transcribe_video():
    params = _tv_params()
    cfg = TranscriberConfig()
    for field, param in _FIELD_TO_PARAM.items():
        assert param in params, f"transcribe_video 缺少形参 {param!r}"
        assert getattr(cfg, field) == params[param].default, (
            f"{field} 默认值漂移：config={getattr(cfg, field)!r} vs "
            f"transcribe_video.{param}={params[param].default!r}"
        )


def test_config_covers_every_transcribe_video_param():
    """反向覆盖：transcribe_video 不应有 config 覆盖不到的具名形参。"""
    params = set(_tv_params())
    supplied_by_provider = {"input_path", "outdir", "base", "progress"}
    mapped = set(_FIELD_TO_PARAM.values())
    missing = params - supplied_by_provider - mapped
    assert not missing, f"transcribe_video 有未被 TranscriberConfig 覆盖的形参：{missing}"


# --------------------------- Provider 透传 ---------------------------

def test_provider_forwards_every_config_field(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_transcribe(input_path, outdir, **kwargs):
        seen["input_path"] = input_path
        seen["outdir"] = outdir
        seen.update(kwargs)
        p = tmp_path / "demo.segments_en.json"
        p.write_text("[]", encoding="utf-8")
        return str(p)

    monkeypatch.setattr("video_translate.transcribe.transcribe_video",
                        fake_transcribe)
    cfg = TranscriberConfig(
        model="medium", chunk=120.0, lang="en", use_vad=True,
        device="cpu", compute_type="int8", align_backend="none",
        separate_vocals=True, vocal_sep_model="htdemucs_ft",
        align_allow_degrade=True,
    )
    out = FasterWhisperProvider().transcribe(
        "v.mp4", str(tmp_path), base="demo", config=cfg)

    assert seen["input_path"] == "v.mp4"
    assert seen["outdir"] == str(tmp_path)
    assert seen["base"] == "demo"
    assert seen["model_name"] == "medium"
    assert seen["chunk"] == 120.0
    assert seen["lang"] == "en"
    assert seen["use_vad"] is True
    assert seen["device"] == "cpu"
    assert seen["compute_type"] == "int8"
    assert seen["align_backend"] == "none"
    assert seen["align_allow_degrade"] is True
    assert seen["separate_vocals"] is True
    assert seen["vocal_sep_model"] == "htdemucs_ft"
    assert out.segments_path.endswith("demo.segments_en.json")


def test_provider_result_segments_satisfy_contract(monkeypatch, tmp_path):
    segs = [{"start": 0.0, "end": 1.0, "text": "hi",
             "words": [], "no_speech_prob": 0.1, "avg_logprob": -0.3,
             "compression_ratio": 1.4}]

    def fake_transcribe(input_path, outdir, **kwargs):
        p = tmp_path / "demo.segments_en.json"
        p.write_text(json.dumps(segs), encoding="utf-8")
        return str(p)

    monkeypatch.setattr("video_translate.transcribe.transcribe_video",
                        fake_transcribe)
    res = FasterWhisperProvider().transcribe(
        "v.mp4", str(tmp_path), base="demo", config=TranscriberConfig())
    assert res.segments == segs
    # 契约：segments_raw 的 required（start/end/text）必须满足
    assert validate_artifact("segments_raw", res.segments, require_carry=False) == []


def test_provider_reads_detected_lang_sidecar(monkeypatch, tmp_path):
    def fake_transcribe(input_path, outdir, **kwargs):
        p = tmp_path / "demo.segments_en.json"
        p.write_text("[]", encoding="utf-8")
        return str(p)

    monkeypatch.setattr("video_translate.transcribe.transcribe_video",
                        fake_transcribe)
    # sidecar 落在 workdir(outdir, base) 下，文件名含内部指纹
    from video_translate.artifacts import workdir

    d = tmp_path / "demo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "demo.abc123.detected_lang.json").write_text(
        json.dumps({"language": "en"}), encoding="utf-8")
    res = FasterWhisperProvider().transcribe(
        "v.mp4", str(tmp_path), base="demo", config=TranscriberConfig())
    assert res.detected_lang == "en"


def test_provider_missing_lang_sidecar_is_none(monkeypatch, tmp_path):
    def fake_transcribe(input_path, outdir, **kwargs):
        p = tmp_path / "demo.segments_en.json"
        p.write_text("[]", encoding="utf-8")
        return str(p)

    monkeypatch.setattr("video_translate.transcribe.transcribe_video",
                        fake_transcribe)
    res = FasterWhisperProvider().transcribe(
        "v.mp4", str(tmp_path), base="demo", config=TranscriberConfig())
    assert res.detected_lang is None


# --------------------------- 就绪声明（ADR-038 D7） ---------------------------

def test_prerequisites_exist_in_caps():
    caps = {c.name for c in capabilities.CAPS}
    for pid in FasterWhisperProvider().prerequisites():
        assert pid in caps, f"prerequisite {pid!r} 不在 capabilities.CAPS 中"


# --------------------------- 协议一致性 ---------------------------

def test_provider_satisfies_protocol():
    p = FasterWhisperProvider()
    assert isinstance(p, ASRProvider)
    assert p.name == "faster-whisper"


def test_transcribe_result_defaults():
    r = TranscribeResult(segments_path="x.json", segments=[])
    assert r.detected_lang is None
