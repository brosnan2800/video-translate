"""ADR-031 D8 — verify 重试次数限制 circuit-breaker。

一次翻译任务中，`cmd_verify` 最多被调用 2 次为严格门（exit 8 on red）。
第 3 次及以后，强制进入报告模式（打印问题列表，exit 0），防止 Agent 陷入
「修 fix 的 fix」振荡。kathy_meta_vlog 本次 ADR-031 修复实际上经过了 3 轮
resegment→generate→verify，第 3 轮仍残 6 窗不可收 —— 该 circuit-breaker
直接来自这次实战教训。

计数器语义：
- `stages.verify.attempts` in vt_state.json
- `run`（完整流水线）重置为 0；`generate`/`resegment`（局部重跑）不重置。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_translate import cli


# --------------------------- fixtures / helpers ---------------------------

def _seg(i: int, text: str, start: float, end: float) -> dict:
    w = [{"start": start, "end": end, "word": t}
         for t in text.split()] or [{"start": start, "end": end, "word": text}]
    return {"index": i, "start": start, "end": end, "text": text, "words": w}


EN = [
    _seg(0, "Hello there.", 0.0, 1.0),
    _seg(1, "General Kenobi.", 1.4, 2.4),
    _seg(2, "You are a bold one.", 2.8, 4.0),
]
ZH = {"0": "你好呀。", "1": "欧比旺·克诺比。", "2": "你是个勇敢的家伙。"}


@pytest.fixture()
def art(tmp_path: Path) -> Path:
    (tmp_path / "demo.segments_en.json").write_text(
        json.dumps(EN), encoding="utf-8")
    (tmp_path / "demo.zh_segments.json").write_text(
        json.dumps(ZH), encoding="utf-8")
    tmp_path.joinpath("demo.mp4").write_bytes(b"")
    return tmp_path


@pytest.fixture()
def no_probe(monkeypatch):
    """Stub out acoustic probing — tests focus on retry counting, not audio."""
    from video_translate.audio_profile import AudioProfile

    def _ok(video, noise="-30dB", d=0.3):
        return AudioProfile(mean_vol=-18.0, max_vol=-3.0,
                            silence_intervals=[(1.0, 1.4), (2.4, 2.8)],
                            duration=5.0, ok=True)
    monkeypatch.setattr(cli, "analyze_audio", _ok)


def _run(art: Path, extra: list[str]) -> int:
    ns = cli.build_parser().parse_args(
        ["verify", "--segments", str(art / "demo.segments_en.json"),
         "--zh", str(art / "demo.zh_segments.json"),
         "--video", str(art / "demo.mp4"), *extra])
    return cli.cmd_verify(ns)


# --------------------------- retry counting -------------------------------

def test_verify_attempt_1_is_strict_and_fails_red(art, no_probe, monkeypatch):
    """第 1 次 verify：strict 默认，红灯应 exit 8。"""
    # coverage 1/3 → content lane 红
    (art / "demo.zh_segments.json").write_text(
        json.dumps({"0": "你好呀。"}), encoding="utf-8")
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    rc = _run(art, [])
    assert rc == cli.EXIT_GATE_FAIL
    st = json.loads((art / "demo.vt_state.json").read_text())
    assert st["stages"]["verify"]["attempts"] == 1


def test_verify_attempt_2_still_strict_fails_red(art, no_probe, monkeypatch):
    """第 2 次 verify：仍 strict，红灯 exit 8。"""
    (art / "demo.zh_segments.json").write_text(
        json.dumps({"0": "你好呀。"}), encoding="utf-8")
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    _run(art, [])  # attempt 1
    rc = _run(art, [])  # attempt 2
    assert rc == cli.EXIT_GATE_FAIL
    st = json.loads((art / "demo.vt_state.json").read_text())
    assert st["stages"]["verify"]["attempts"] == 2


def test_verify_attempt_3_forces_report_only(art, no_probe, monkeypatch):
    """ADR-031 D8 — 第 3 次 verify：retry limit reached，强制降级报告模式 exit 0。"""
    (art / "demo.zh_segments.json").write_text(
        json.dumps({"0": "你好呀。"}), encoding="utf-8")  # 持续红灯
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    _run(art, [])  # attempt 1 → exit 8
    _run(art, [])  # attempt 2 → exit 8
    rc = _run(art, [])  # attempt 3 → 强制 report-only
    assert rc == cli.EXIT_OK  # not EXIT_GATE_FAIL
    st = json.loads((art / "demo.vt_state.json").read_text())
    assert st["stages"]["verify"]["attempts"] == 3


def test_verify_attempt_4_plus_still_report_only(art, no_probe, monkeypatch):
    """超过 3 次：始终报告模式。"""
    (art / "demo.zh_segments.json").write_text(
        json.dumps({"0": "你好呀。"}), encoding="utf-8")
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    for _ in range(3):
        _run(art, [])
    rc = _run(art, [])  # attempt 4
    assert rc == cli.EXIT_OK
