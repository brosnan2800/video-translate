"""advisor_vocal_sep — 是否启用 Demucs 人声分离（--separate-vocals）。

为什么它是 CRITICAL
------------------
人声分离是 Phase 0→1 之间**唯一"选错就整条下游全废"**的决策：
- 该分离却没分离 → BGM/哄笑压住人声 → 转写漏句、幻觉，后面翻译/生成全建在坏地基上。
- 不该分离却分离 → 多跑一遍重模型、且 Demucs 输出时长若与原音频不一致会引入偏移风险。
两种错都只能通过**重跑整条流水线**来修，代价远高于在 Phase 0 停 30 秒问一句。
故 CRITICAL=True：未拍板不许 confirm。

判据（复刻仓库 cli.py::cmd_doctor 的真实阈值）
------------------------------------------
静音占比 silence_fraction < CLEAN_SILENCE_FRACTION(0.10) 视为「连续噪声 / 高密度音频」
→ 建议 --separate-vocals。仓库现状只 print 一句 "vocal separation: RECOMMENDED"，
本 advisor 把它变成可签字、可渲染成命令行的结构化决策项。
"""
from __future__ import annotations

from typing import Any

NAME = "separate_vocals"
CRITICAL = True
FLAG = "--separate-vocals"

CLEAN_SILENCE_FRACTION = 0.10  # 与仓库 cli.py 保持一致


def _silence_fraction(intervals: list[tuple[float, float]], duration: float) -> float:
    if not duration or duration <= 0:
        return 0.0
    total = sum(max(0.0, float(e) - float(s)) for s, e in intervals)
    return min(1.0, total / duration)


def advise(video: str) -> dict[str, Any]:
    """调仓库真实 analyze_audio 算静音占比；仓库包不可用时明确标 unknown（不静默假装）。"""
    try:
        from video_translate.audio_profile import analyze_audio  # type: ignore
        from video_translate.ffmpeg_utils import probe_duration  # type: ignore
    except Exception as e:  # noqa: BLE001
        return {
            "recommended": None,
            "reason": f"无法导入仓库音频分析模块（{type(e).__name__}），程序给不出建议，必须人工判断",
            "detail": {"status": "unknown"},
        }

    try:
        prof = analyze_audio(video)
        dur = float(probe_duration(video) or 0.0)
        sf = _silence_fraction(getattr(prof, "silence_intervals", []) or [], dur)
    except Exception as e:  # noqa: BLE001
        return {
            "recommended": None,
            "reason": f"音频分析失败（{type(e).__name__}: {e}），必须人工判断",
            "detail": {"status": "error"},
        }

    rec = sf < CLEAN_SILENCE_FRACTION
    reason = (
        f"silence_fraction={sf:.3f} < {CLEAN_SILENCE_FRACTION} → 连续噪声/高密度音频，"
        f"建议启用人声分离"
        if rec else
        f"silence_fraction={sf:.3f} ≥ {CLEAN_SILENCE_FRACTION} → 静音间隔充足，"
        f"可不分离（省一次重模型）"
    )
    return {
        "recommended": rec,
        "reason": reason,
        "detail": {
            "silence_fraction": round(sf, 4),
            "threshold": CLEAN_SILENCE_FRACTION,
            "duration_s": round(dur, 2),
            "mean_vol": getattr(prof, "mean_vol", None),
            "max_vol": getattr(prof, "max_vol", None),
        },
    }
