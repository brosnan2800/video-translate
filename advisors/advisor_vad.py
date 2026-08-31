"""advisor_vad — VAD 路由模式（裸跑 / --vad / --vad --vad-threshold 0.1 / --adaptive-vad）。

判据（复刻仓库 audio_profile.recommend_vad 的真实阈值）
--------------------------------------------------
    LOW_MEAN_DB = -20    平均电平低
    LOW_MAX_DB  = -5     峰值电平低
电平偏低 → "--vad --vad-threshold 0.1"（放宽门限，避免把小声人声当静音丢掉）
电平正常 → "--vad"（锚定静音）

CRITICAL=False 的理由
--------------------
VAD 选错的代价是**可局部修复的**：漏句会在 Phase 4 声学 lane 以 uncovered-audio
形式暴露，重跑单个 chunk 即可；不像人声分离那样污染整条下游。所以它给默认值、
允许直接沿用建议，不强制人拍板。
"""
from __future__ import annotations

from typing import Any

NAME = "vad_mode"
CRITICAL = False
FLAG = None  # 值本身就是完整 flag 串，用自定义 render

LOW_MEAN_DB = -20.0
LOW_MAX_DB = -5.0

CHOICES = ("bare", "vad", "vad-low", "adaptive")

_RENDER = {
    "bare": [],
    "vad": ["--vad"],
    "vad-low": ["--vad", "--vad-threshold", "0.1"],
    "adaptive": ["--adaptive-vad"],
}


def advise(video: str) -> dict[str, Any]:
    try:
        from video_translate.audio_profile import analyze_audio  # type: ignore
    except Exception as e:  # noqa: BLE001
        return {
            "recommended": None,
            "reason": f"无法导入仓库音频分析模块（{type(e).__name__}），必须人工选择",
            "detail": {"choices": list(CHOICES), "status": "unknown"},
        }

    try:
        prof = analyze_audio(video)
        mean_vol = float(getattr(prof, "mean_vol", 0.0) or 0.0)
        max_vol = float(getattr(prof, "max_vol", 0.0) or 0.0)
    except Exception as e:  # noqa: BLE001
        return {
            "recommended": None,
            "reason": f"音频分析失败（{type(e).__name__}: {e}），必须人工选择",
            "detail": {"choices": list(CHOICES), "status": "error"},
        }

    low = mean_vol < LOW_MEAN_DB or max_vol < LOW_MAX_DB
    rec = "vad-low" if low else "vad"
    reason = (
        f"mean={mean_vol:.1f}dB / max={max_vol:.1f}dB 低于阈值"
        f"（{LOW_MEAN_DB}/{LOW_MAX_DB}）→ 放宽 VAD 门限至 0.1，避免小声人声被丢弃"
        if low else
        f"mean={mean_vol:.1f}dB / max={max_vol:.1f}dB 电平正常 → 用 --vad 锚定静音"
    )
    return {
        "recommended": rec,
        "reason": reason,
        "detail": {
            "choices": list(CHOICES),
            "mean_vol": mean_vol,
            "max_vol": max_vol,
            "thresholds": {"LOW_MEAN_DB": LOW_MEAN_DB, "LOW_MAX_DB": LOW_MAX_DB},
        },
    }


def render(chosen: Any) -> list[str]:
    if chosen is None:
        return []
    if chosen not in _RENDER:
        raise ValueError(f"vad_mode 非法值 {chosen!r}，可选 {CHOICES}")
    return list(_RENDER[chosen])
