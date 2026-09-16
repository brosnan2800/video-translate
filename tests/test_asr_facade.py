"""ADR-038 D5 第二步（A 块）—— ASR 层门面 ``run_asr`` 契约测试。

锁定的行为（必须与搬迁前 ``cli.cmd_transcribe`` 的编排逐项等价）：

  1. 编排顺序：``人声分离 → 引擎转写 → apply_merge → fill_gaps``
  2. 开关：``merge=False`` 不调 ``apply_merge``；``audit=False`` 不调 ``fill_gaps``
  3. 分离产物（``audio_source`` / ``vsep_*``）透传给引擎与 ``fill_gaps``
  4. ``run_asr`` **不触碰控制面**（state 落盘归 cli 层的记账，不属 ① ASR 层）
  5. 返回 ``AsrOutcome``（段路径 / 段数据 / 语言 / 分离产物）

埋点方式：patch **源模块属性**（``video_translate.merge.apply_merge`` 等）——
不 patch ``asr`` 内部的局部引用，避免绑定点漂移（.clinerules/05 的 mock 规范）。
产物路径一律走 ``artifacts.artifact_path``（ADR-037: ``<outdir>/<base>/``），
与 ``run_asr`` 内部一致。
"""
from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from video_translate.artifacts import artifact_path
from video_translate.asr import (
    AsrOutcome,
    AsrRequest,
    TranscriberConfig,
    run_asr,
)


def _write(path: str, obj) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


class _SpyProvider:
    """记录调用，并经 ``transcribe`` 写出一份最小 segments_en.json。"""

    name = "spy"

    def __init__(self, calls: list, segs_path: str, out_segments: list):
        self._calls = calls
        self._segs_path = segs_path
        self._out = out_segments
        self.seen_config = None

    def prerequisites(self) -> tuple[str, ...]:
        return ()

    def transcribe(self, input_path, outdir, *, base, config, progress=print):
        from video_translate.asr import TranscribeResult

        self._calls.append("transcribe")
        self.seen_config = config
        _write(self._segs_path, self._out)
        return TranscribeResult(segments_path=self._segs_path,
                                segments=list(self._out),
                                detected_lang="en")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """最小 ① 层环境：假视频 + spy provider + 被记录的后处理步骤。"""
    calls: list = []
    video = tmp_path / "in.mp4"
    video.write_bytes(b"")
    outdir = str(tmp_path)
    base = "demo"

    # 产物路径走 artifacts 契约（ADR-037 的 <outdir>/<base>/），与门面内部一致
    segs_path = artifact_path("segments", outdir, base)

    raw_segments = [
        {"start": 0.0, "end": 1.0, "text": "hello", "no_speech_prob": 0.1,
         "avg_logprob": -0.3, "compression_ratio": 1.2},
    ]
    _write(segs_path, raw_segments)

    def _fake_merge(segments_path, *, raw_path, **kw):
        calls.append(("merge", kw))
        _write(segments_path, raw_segments)
        _write(raw_path, raw_segments)

    def _fake_fill_gaps(input_path, segs, **kw):
        calls.append(("fill_gaps", kw))
        return list(segs)

    # 独立静音参照的兜底路径不得触发真实 ffmpeg（Spec 19: 参照取自原始输入）
    class _Prof:
        ok = False
        silence_intervals: list = []

    monkeypatch.setattr("video_translate.audio_profile.analyze_audio",
                        lambda *a, **k: _Prof())
    monkeypatch.setattr("video_translate.merge.apply_merge", _fake_merge)
    monkeypatch.setattr("video_translate.fill_gaps.fill_gaps", _fake_fill_gaps)

    provider = _SpyProvider(calls, segs_path, raw_segments)
    request = AsrRequest(
        input_path=str(video), outdir=outdir, base=base,
        config=TranscriberConfig(),
    )
    return {
        "calls": calls, "provider": provider, "request": request,
        "segs_path": segs_path,
    }


def _kinds(calls: list) -> list:
    return [c if isinstance(c, str) else c[0] for c in calls]


# --------------------------- 1. 编排顺序 ---------------------------

def test_run_asr_orchestration_order(env):
    """人声分离 → 转写 → merge → fill_gaps（顺序即契约）。"""
    out = run_asr(env["request"], provider=env["provider"],
                  progress=lambda *a: None)

    assert _kinds(env["calls"]) == ["transcribe", "merge", "fill_gaps"]
    assert isinstance(out, AsrOutcome)
    assert out.segments_path == env["segs_path"]
    assert out.detected_lang == "en"


# --------------------------- 2. 开关 ---------------------------

def test_run_asr_merge_off_skips_apply_merge(env):
    req = replace(env["request"], merge=False)
    run_asr(req, provider=env["provider"], progress=lambda *a: None)

    assert _kinds(env["calls"]) == ["transcribe", "fill_gaps"]


def test_run_asr_audit_off_skips_fill_gaps(env):
    req = replace(env["request"], audit=False)
    run_asr(req, provider=env["provider"], progress=lambda *a: None)

    assert _kinds(env["calls"]) == ["transcribe", "merge"]


# --------------------------- 3. 无分离时 audio_source 为 None ---------------------------

def test_run_asr_without_separation_passes_none_audio_source(env):
    out = run_asr(env["request"], provider=env["provider"],
                  progress=lambda *a: None)

    assert out.audio_source is None
    assert out.vsep_backend == "demucs"
    # 引擎侧拿到的 config.audio_source 也应为 None（用原输入）
    assert env["provider"].seen_config.audio_source is None
    # fill_gaps 同样收到 None
    fg = next(c[1] for c in env["calls"]
              if isinstance(c, tuple) and c[0] == "fill_gaps")
    assert fg["audio_source"] is None
    # 补洞恒为裸跑（ADR-016 T2a）
    assert fg["use_vad"] is False


# --------------------------- 4. 后处理参数透传 ---------------------------

def test_run_asr_forwards_merge_thresholds(env):
    req = replace(env["request"], merge_max_dur=9.0, merge_max_gap=0.7,
                  merge_max_chars=30)
    run_asr(req, provider=env["provider"], progress=lambda *a: None)

    kw = next(c[1] for c in env["calls"]
              if isinstance(c, tuple) and c[0] == "merge")
    assert kw["max_dur"] == 9.0
    assert kw["max_gap"] == 0.7
    assert kw["split_max_chars"] == 30


# --------------------------- 5. 不触碰控制面 ---------------------------

def test_run_asr_does_not_write_state(env, monkeypatch):
    """run_asr 只做 ① 层的事；state 落盘是 cli 层记账，必须一次都不写。"""
    touched: list = []
    monkeypatch.setattr("video_translate.state.ensure_state",
                        lambda *a, **k: touched.append("ensure_state"))
    monkeypatch.setattr("video_translate.state.record_stage",
                        lambda *a, **k: touched.append("record_stage"))

    run_asr(env["request"], provider=env["provider"], progress=lambda *a: None)
    assert touched == []


# --------------------------- 6. 返回段数据 ---------------------------

def test_run_asr_returns_segments_payload(env):
    out = run_asr(env["request"], provider=env["provider"],
                  progress=lambda *a: None)
    assert isinstance(out.segments, list) and out.segments
    assert out.segments[0]["text"] == "hello"
