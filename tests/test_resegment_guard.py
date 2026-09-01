"""ADR-031 D1/D2/D8 — resegment 产出守卫与 origin 标记 / verify retry counting。

kathy_meta_vlog 事故：resegment 轮 1 解码出 "We'll be right back."
(no_speech_prob=0.90625) 直接拼接进字幕——fill_gaps 的恢复段守卫
(`_is_recovered_hallucination`, ADR-021) 不覆盖 resegment 路径，Whisper 自判
非语音的幻觉逃过全部防线（同类漏网还有 "Wait." nsp=0.851）。本组测试固化：

  - resegment 拼接前必须过同一道守卫，拦截必须打印可见（不许静默）；
  - 保留段必须打 origin="resegment"（恢复段类别对下游可见）；
  - 阈值与 fill_gaps 守卫一致（no_speech_thr=0.6 / avg_logprob_thr=-1.0 / 
    短段重叠 >0.12s）。
"""
from __future__ import annotations

import argparse
import json

from video_translate.cli import EXIT_OK, cmd_resegment

BASE_SEG = [{"start": 0.0, "end": 1.0, "text": "Hello there."}]


def _seg(text: str, start: float, end: float, *, nsp=None, alp=None, words=None):
    s: dict = {"start": start, "end": end, "text": text}
    if nsp is not None:
        s["no_speech_prob"] = nsp
    if alp is not None:
        s["avg_logprob"] = alp
    if words is not None:
        s["words"] = words
    return s


def _run_resegment(tmp_path, monkeypatch, decoded, windows=("2.0-3.0",)):
    p = tmp_path / "demo.segments_en.json"
    p.write_text(json.dumps(BASE_SEG), encoding="utf-8")
    # 守卫行为测试不依赖 ffmpeg 存在性（toolchain 全局缓存未初始化时
    # _require_ffmpeg 会以 EXIT_MISSING_DEP 提前返回）——显式 bypass。
    monkeypatch.setattr("video_translate.cli._require_ffmpeg", lambda: None)
    monkeypatch.setattr(
        "video_translate.transcribe.transcribe_window",
        lambda *a, **k: [dict(s) for s in decoded])
    args = argparse.Namespace(
        segments=str(p), video="demo.mp4", windows=list(windows), lang="en",
        vad=False, model=None, threads=4, device=None, compute_type=None,
        separate_vocals=None, demucs_model=None)
    rc = cmd_resegment(args)
    merged = json.loads(p.read_text(encoding="utf-8"))
    return rc, merged


def test_resegment_drops_high_no_speech_hallucination(tmp_path, monkeypatch, capsys):
    """nsp>=0.6 的窗口解码产出必须在拼接前被守卫丢弃（We'll be right back. 0.906 类）。"""
    phantom = _seg("We'll be right back.", 2.0, 3.4, nsp=0.90625, alp=-0.716,
                   words=[{"start": 2.0, "end": 3.4, "word": "We'll"}])
    rc, merged = _run_resegment(tmp_path, monkeypatch, [phantom])
    assert rc == EXIT_OK
    assert len(merged) == 1                     # 原段保留，幻觉段未入列
    out = capsys.readouterr().out
    assert "hallucination guard" in out         # 拦截必须可见，不能静默
    assert "We'll be right back." in out
    assert "0.90625" in out


def test_resegment_keeps_clean_segment_with_origin_tag(tmp_path, monkeypatch):
    clean = _seg("Not going to make it.", 2.0, 3.4, nsp=0.1, alp=-0.4)
    rc, merged = _run_resegment(tmp_path, monkeypatch, [clean])
    assert rc == EXIT_OK
    assert len(merged) == 2
    new = merged[1]
    assert new["origin"] == "resegment"         # ADR-031 D2: 恢复段类别可见
    assert new["lang"] == "en"


def test_resegment_guard_threshold_boundary(tmp_path, monkeypatch):
    """0.59 保留 / 0.61 丢弃——阈值与 fill_gaps 守卫一致（no_speech_thr=0.6）。"""
    _, merged = _run_resegment(tmp_path, monkeypatch,
                               [_seg("Edge low.", 2.0, 3.0, nsp=0.59)])
    assert len(merged) == 2
    _, merged = _run_resegment(tmp_path, monkeypatch,
                               [_seg("Edge high.", 2.0, 3.0, nsp=0.61)])
    assert len(merged) == 1


def test_resegment_guard_low_logprob_signal(tmp_path, monkeypatch):
    """avg_logprob < -1.0 单独命中（C2 信号，老缓存无 no_speech_prob 时兜底）。"""
    _, merged = _run_resegment(
        tmp_path, monkeypatch,
        [_seg("Subtitles by the community.", 2.0, 3.0, alp=-1.4)])
    assert len(merged) == 1


def test_resegment_guard_overlap_signal(tmp_path, monkeypatch):
    """短恢复段（<=4 词）骑在保留段音频上 -> overlap 信号丢弃（ADR-021 信号 A）。"""
    p = tmp_path / "demo.segments_en.json"
    p.write_text(json.dumps(BASE_SEG), encoding="utf-8")
    rider = _seg("Yeah right.", 0.8, 2.0, nsp=0.1,
                 words=[{"start": 0.8, "end": 1.2, "word": "Yeah"},
                        {"start": 1.2, "end": 1.6, "word": "right."},
                        {"start": 1.6, "end": 2.0, "word": "and"}])
    monkeypatch.setattr("video_translate.cli._require_ffmpeg", lambda: None)
    monkeypatch.setattr("video_translate.transcribe.transcribe_window",
                        lambda *a, **k: [dict(rider)])
    args = argparse.Namespace(
        segments=str(p), video="demo.mp4", windows=["1.5-3.0"], lang="en",
        vad=False, model=None, threads=4, device=None, compute_type=None,
        separate_vocals=None, demucs_model=None)
    assert cmd_resegment(args) == EXIT_OK
    merged = json.loads(p.read_text(encoding="utf-8"))
    assert len(merged) == 1                     # rider 与 kept [0,1] 重叠 0.2s -> 丢弃


# --------------------------- D8: verify retry reset ----------------------

def test_resegment_resets_verify_attempts_on_new_run(tmp_path, monkeypatch):
    """`resegment` 不应重置 verify.attempts（局部重跑不清零计数）。

    kathy_meta_vlog 教训：3 轮 resegment→generate→verify，第 3 轮仍残 6 窗。
    若 resegment 重置计数，就是绕开 D8 circuit-breaker —— 必须禁止。
    """
    import video_translate.state as vt_state
    # 模拟前两次 verify 已累积
    vt_state.increment_verify_attempts(str(tmp_path), "demo")  # 1
    vt_state.increment_verify_attempts(str(tmp_path), "demo")  # 2
    assert vt_state.load(str(tmp_path), "demo")["stages"]["verify"]["attempts"] == 2

    # resegment后计数不应归零
    rc, merged = _run_resegment(
        tmp_path, monkeypatch,
        [_seg("Clean.", 2.0, 3.0, nsp=0.1)])
    st2 = vt_state.load(str(tmp_path), "demo")
    assert st2["stages"]["verify"]["attempts"] == 2  # 未重置


def test_run_command_resets_verify_attempts(tmp_path, monkeypatch):
    """模拟 run 重置计数器（完整流水线重跑 = 新任务）。"""
    import video_translate.state as vt_state
    # 设置计数到 5（模拟多次 verify）
    for _ in range(5):
        vt_state.increment_verify_attempts(str(tmp_path), "demo")
    assert vt_state.load(str(tmp_path), "demo")["stages"]["verify"]["attempts"] == 5

    # 模拟 run 重置
    vt_state.reset_verify_attempts(str(tmp_path), "demo")

    st3 = vt_state.load(str(tmp_path), "demo")
    assert st3["stages"]["verify"]["attempts"] == 0
