"""ADR-034 §6.3（G3）—— 四组进入条件单测（TDD，demucs 全部 mock）。

G3 = 局部 separate-vocals，只兜「强 BGM / 连续噪声掩盖真音」，**不兜笑声**
（笑声是人声，demucs 分不开，ADR-034 §1.4 能力边界）。四组门控：

  组1 硬前置：review 判 MISSING ∧ G1 已跑仍空 ∧ G2 已跑仍空 ∧ 窗长 ≥5s
  组2 场景适配双门：(c) 画像预筛（连续噪声）→ (a) demucs 能量复核（other 轨有货）
  组3 性能预算：总窗长 < 30% 视频 且 < 10min（取小），超限只取最可疑
  组4 自校验：重解码后再过双信号 review，仍 MISSING → 标「当前架构救不了」
"""
from __future__ import annotations

import pytest

from video_translate.fill_gaps import (
    g3_energy_verdict,
    g3_prescreen,
    g3_self_check_ok,
    group_short_windows,
    select_g3_windows,
)


# --------------------------------------------------------------------------- #
# 组1：短窗合并（demucs <5s 质量差 → 与邻近可疑窗合并一起分离）
# --------------------------------------------------------------------------- #

def test_group_short_windows_merges_adjacent_under_min():
    # (0,2)+(2.1,4) 相邻（gap 0.1s）合并成 (0,4)；(10,13) 隔 6s 不合并
    out = group_short_windows([(0.0, 2.0), (2.1, 4.0), (10.0, 13.0)],
                              min_dur=5.0, max_merge_gap=2.0)
    assert out == [(0.0, 4.0), (10.0, 13.0)]


def test_group_short_windows_keeps_long_window_intact():
    # 已 ≥ min_dur 的窗不强行并邻居（避免把干净语音拖进分离）
    out = group_short_windows([(0.0, 6.0), (7.0, 9.0)],
                              min_dur=5.0, max_merge_gap=2.0)
    assert out == [(0.0, 6.0), (7.0, 9.0)]


def test_group_short_windows_sorts_and_handles_empty():
    assert group_short_windows([], min_dur=5.0) == []
    # 输入未排序也要稳定
    out = group_short_windows([(10.0, 13.0), (0.0, 2.0)], min_dur=5.0)
    assert out == [(0.0, 2.0), (10.0, 13.0)]


# --------------------------------------------------------------------------- #
# 组2(c)：画像预筛 —— 连续噪声/BGM 嫌疑窗才进入 demucs 试跑
# --------------------------------------------------------------------------- #

def test_g3_prescreen_continuous_noise_passes():
    assert g3_prescreen(0.02) is True      # 几乎无静音气口 = 连续噪声/BGM
    assert g3_prescreen(0.09) is True      # < 10% 阈值


def test_g3_prescreen_pause_rich_window_blocked():
    assert g3_prescreen(0.50) is False     # 一半是静音 = 干净对话，不是 BGM 掩盖


def test_g3_prescreen_unknown_lets_energy_gate_decide():
    # 画像未知时不替 demucs 做主：放行，由 (a) 能量复核兜底（双门设计）
    assert g3_prescreen(None) is True


# --------------------------------------------------------------------------- #
# 组2(a)：demucs 能量复核 —— other 轨有货才说明真剥下 BGM
# --------------------------------------------------------------------------- #

def test_g3_energy_verdict_bgm_present_proceeds():
    # other（伴奏）能量接近 vocals → 真有 BGM 被剥下来
    assert g3_energy_verdict(-20.0, -18.0) is True


def test_g3_energy_verdict_laughter_skips_demucs():
    # 笑声留 vocals、other 轨近乎静音 → 没东西可剥，跳过（不白跑 demucs）
    assert g3_energy_verdict(-20.0, -70.0) is False


def test_g3_energy_verdict_near_silent_other_skips():
    assert g3_energy_verdict(-30.0, -50.0) is False  # other 低于下限 -45dB


def test_g3_energy_verdict_probe_failure_is_conservative():
    assert g3_energy_verdict(None, -20.0) is False
    assert g3_energy_verdict(-20.0, None) is False


# --------------------------------------------------------------------------- #
# 组3：性能预算（总时长 < 30% 视频 且 < 10min，取小）
# --------------------------------------------------------------------------- #

def _cands(spans):
    return [{"start": s, "end": e, "score": e - s, "index": i}
            for i, (s, e) in enumerate(spans)]


def test_select_g3_windows_frac_budget():
    # 视频 100s → 30% = 30s 上限；5 个 10s 窗只能取 3 个
    cands = _cands([(0, 10), (10, 20), (20, 30), (30, 40), (40, 50)])
    kept, dropped = select_g3_windows(cands, total_duration=100.0)
    assert len(kept) == 3
    assert len(dropped) == 2
    assert sum(c["end"] - c["start"] for c in kept) <= 30.0


def test_select_g3_windows_abs_budget_caps_at_ten_minutes():
    # 视频很长（10000s），30% = 3000s，但绝对上限 600s 生效 → 只取 1 个 400s 窗
    cands = _cands([(0, 400), (400, 800), (800, 1200)])
    kept, dropped = select_g3_windows(cands, total_duration=10000.0)
    assert len(kept) == 1
    assert sum(c["end"] - c["start"] for c in kept) <= 600.0


def test_select_g3_windows_prefers_most_suspicious():
    # 视频 100s → 预算 30s；两窗各 20s，装不下第二个 → 必须留最可疑的那窗
    cands = [
        {"start": 0, "end": 20, "score": 0.1, "index": 0},    # 低可疑
        {"start": 20, "end": 40, "score": 0.95, "index": 1},  # 高可疑
    ]
    kept, dropped = select_g3_windows(cands, total_duration=100.0)
    assert [c["index"] for c in kept] == [1]
    assert [c["index"] for c in dropped] == [0]


def test_select_g3_windows_empty_and_no_fit():
    assert select_g3_windows([], total_duration=100.0) == ([], [])
    # 单窗就超预算 → 一个都不处理（预算为硬上限，验收要求）
    cands = _cands([(0, 50)])
    kept, dropped = select_g3_windows(cands, total_duration=100.0)  # 上限 30s
    assert kept == [] and len(dropped) == 1


# --------------------------------------------------------------------------- #
# 组4：自校验退出 —— 重解码后仍 MISSING = 当前架构救不了
# --------------------------------------------------------------------------- #

def test_g3_self_check_ok_clean_recovery():
    segs = [{"start": 0.0, "end": 2.0, "text": "clearly audible speech",
             "no_speech_prob": 0.1, "avg_logprob": -0.4}]
    # 无静音参照 → 全窗视为有能量；置信度干净 → 判定救回
    assert g3_self_check_ok(segs, []) is True


def test_g3_self_check_fails_when_still_missing():
    segs = [{"start": 0.0, "end": 2.0, "text": "still muffled",
             "no_speech_prob": 0.88, "avg_logprob": -0.5}]
    # 有能量 + 高 no_speech → 仍 MISSING → 不可救回
    assert g3_self_check_ok(segs, []) is False


def test_g3_self_check_empty_is_not_rescue():
    # 重解码什么都没出 = 没救回（不算通过校验）
    assert g3_self_check_ok([], []) is False


def test_g3_self_check_window_in_silence_is_not_missing():
    # 整窗落在静音里 → 不判 MISSING（不该拿静音窗当漏译去救）
    segs = [{"start": 1.0, "end": 2.0, "text": "x", "no_speech_prob": 0.9}]
    assert g3_self_check_ok(segs, [(0.0, 5.0)]) is True


# --------------------------------------------------------------------------- #
# 组2(c) 窗级探测（Option B）：预筛依据必须来自「窗」，不是整片
# --------------------------------------------------------------------------- #

def test_probe_window_silence_fraction_is_window_scoped(monkeypatch):
    from video_translate import audio_profile

    seen: dict[str, object] = {}

    class _FakeProf:
        ok = True
        duration = 10.0
        silence_intervals = [(0.0, 2.0), (3.0, 4.0)]   # 3s / 10s = 0.30

    monkeypatch.setattr(
        audio_profile, "extract_chunk",
        lambda src, dst, start, dur: seen.update(
            {"src": src, "start": start, "dur": dur}))
    monkeypatch.setattr(
        audio_profile, "analyze_audio",
        lambda path, noise="-30dB", d=0.3: seen.update({"probed": path})
        or _FakeProf())

    sf = audio_profile.probe_window_silence_fraction("video.mp4", 10.0, 20.0)
    assert sf == pytest.approx(0.30)
    # 抽的是入参指定的窗，探测对象也是抽出来的窗（不是整片）
    assert seen["start"] == 10.0
    assert seen["dur"] == pytest.approx(10.0)
    assert seen["probed"] != "video.mp4"


def test_probe_window_silence_fraction_failure_returns_none(monkeypatch):
    from video_translate import audio_profile

    def _boom(*a, **k):
        raise RuntimeError("ffmpeg missing")

    monkeypatch.setattr(audio_profile, "extract_chunk", _boom)
    # 预筛是粗门，探测失败不能拖垮 G3：返回 None → 放行给 (a) 能量复核
    assert audio_profile.probe_window_silence_fraction("v.mp4", 0.0, 5.0) is None


# --------------------------------------------------------------------------- #
# G3 设备自动探测：Whisper 已在显存，看「剩余」够不够，不盲目抢显存
# --------------------------------------------------------------------------- #

def test_pick_separation_device_honors_explicit(monkeypatch):
    from video_translate import vocal_sep

    monkeypatch.setattr(vocal_sep, "cuda_free_memory_gb", lambda: 8.0)
    assert vocal_sep.pick_separation_device("cpu") == "cpu"
    assert vocal_sep.pick_separation_device("cuda") == "cuda"


def test_pick_separation_device_without_cuda(monkeypatch):
    from video_translate import vocal_sep

    monkeypatch.setattr(vocal_sep, "cuda_free_memory_gb", lambda: None)
    assert vocal_sep.pick_separation_device("auto") == "cpu"


def test_pick_separation_device_enough_vram_uses_gpu(monkeypatch):
    from video_translate import vocal_sep

    monkeypatch.setattr(vocal_sep, "cuda_free_memory_gb", lambda: 4.0)
    # 需要 1.5 + 0.5 = 2.0GiB，剩 4.0 → 上 GPU
    assert vocal_sep.pick_separation_device("auto") == "cuda"


def test_pick_separation_device_tight_vram_degrades_to_cpu(monkeypatch):
    from video_translate import vocal_sep

    monkeypatch.setattr(vocal_sep, "cuda_free_memory_gb", lambda: 1.0)
    # 只剩 1.0GiB < 2.0GiB → 退 CPU：不 OOM，也不去重载 Whisper
    assert vocal_sep.pick_separation_device("auto") == "cpu"
