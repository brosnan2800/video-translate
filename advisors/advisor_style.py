"""advisor_style — 翻译风格轨（film / literal / bilingual_study）。

一条诚实的设计声明
----------------
**风格是「意图」，不是「可计算量」。** 同一段素材，你想做影视二创就选 film，
想做逐句精读就选 bilingual_study —— 这取决于你要发什么内容，程序无从推断。
所以本 advisor 的 ``recommended`` 永远返回 None（不假装能算），只做两件事：
  1. 把三条可选轨及其适用场景摊在你面前，避免你忘了有这个选项；
  2. CRITICAL=True 强制你在 Phase 1 之前拍板，而不是让默认值悄悄生效。

对应仓库真实实现（src/video_translate/translate.py）
------------------------------------------------
``prepare_translate_task`` 会把 ``STYLE_PERSONAS[style].persona`` 与
``STYLE_GUIDELINES[style]`` 拼进 ``translate_task.json`` 的 persona / guidelines。
注意：这只是**注入指令**，仓库里没有任何 gate 校验 agent 真的照该风格翻了 ——
风格的最终把关仍是你末道人工审。本 advisor 只保证「你确实主动选过」。
"""
from __future__ import annotations

from typing import Any

NAME = "style"
CRITICAL = True
FLAG = "--style"

CHOICES = ("film", "literal", "bilingual_study")

_DESC = {
    "film": "影视二创：口语化、重节奏与观感，允许意译换说法",
    "literal": "忠实直译：不增不减、结构对齐、术语严谨，适合技术/法律/纪实",
    "bilingual_study": "双语精读：直译为主 + 生词注记，句式可回映英文，适合学习向",
}


def advise(video: str) -> dict[str, Any]:
    return {
        "recommended": None,  # 有意为之：风格是意图，程序不猜
        "reason": "风格取决于你要发布的内容形态，程序无法推断，必须人工指定",
        "detail": {"choices": list(CHOICES), "descriptions": _DESC,
                   "note": "支持逗号分隔多风格（如 film,literal），会各产一份 task 与译稿"},
    }


def render(chosen: Any) -> list[str]:
    if chosen is None:
        return []
    parts = [s.strip() for s in str(chosen).split(",") if s.strip()]
    bad = [p for p in parts if p not in CHOICES]
    if bad:
        raise ValueError(f"style 非法值 {bad}，可选 {CHOICES}")
    return ["--style", ",".join(parts)]
