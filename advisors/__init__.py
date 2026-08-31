"""advisors — Phase 0 决策项注册表（自动发现，开闭原则）。

设计目的
--------
Phase 0（doctor）已经能算出 VAD 路由、人声分离这类建议，但仓库现状只 ``print``
一句话，执行与否靠人/agent 自觉 —— 这是「软约束」。本包把每一条建议升级成
**结构化决策项**，落盘进 ``<base>.preflight_decision.json``，由人签字确认后，
Phase 1 的命令参数**直接从决策单渲染**（而非靠人记得传 flag）。

为什么用注册表（而不是把逻辑写进一个大函数）
------------------------------------------
「每次翻译发现新问题 → 往流程里加逻辑」是 shotgun surgery 的温床。这里改成：
**新增一个决策项 = 在本目录新建一个 ``advisor_*.py``，核心代码一字不改。**
注册表在 import 时自动发现所有 ``advisor_*.py``，物理上碰不到旧决策项。

Advisor 契约（每个 advisor_*.py 必须导出）
----------------------------------------
    NAME: str                 决策项 key，写进 decision["items"] 的键名
    CRITICAL: bool            True = 未拍板不许 confirm（人必须选）
    FLAG: str | None          对应 CLI flag（None = 不渲染进命令行，如 style 用 --style=值）
    def advise(video: str) -> dict:
        返回 {"recommended": <值>, "reason": "<为什么>", "detail": {...}}
        - recommended 可为 bool / str / None
        - None 表示「程序无法推断，必须人定」（如翻译风格 = 意图，不是可计算量）
    def render(chosen) -> list[str]     可选。把已确认的值渲染成 CLI 参数片段
                                        缺省行为见 _default_render

诚实边界
--------
advisor 只负责「算建议 + 给理由」，**不负责决定**。决定权在人（confirm），
执行权在渲染出的命令行。三者分离，任何一环都可审计。
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

__all__ = ["discover", "advise_all", "render_flags", "AdvisorError"]


class AdvisorError(RuntimeError):
    """advisor 加载或执行失败（不静默吞掉——静默降级会让人误以为已检查过）。"""


def _default_render(flag: str | None, chosen: Any) -> list[str]:
    """缺省渲染：bool -> 有/无该 flag；str -> ``flag value``；None -> 空。"""
    if flag is None or chosen is None:
        return []
    if isinstance(chosen, bool):
        return [flag] if chosen else []
    return [flag, str(chosen)]


def discover() -> list[Any]:
    """自动发现本目录下所有 ``advisor_*.py`` 模块，按 NAME 排序返回。"""
    mods: list[Any] = []
    pkg_dir = Path(__file__).parent
    for info in pkgutil.iter_modules([str(pkg_dir)]):
        if not info.name.startswith("advisor_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as e:  # noqa: BLE001
            raise AdvisorError(f"advisor 加载失败 {info.name}: {e}") from e
        for attr in ("NAME", "advise"):
            if not hasattr(mod, attr):
                raise AdvisorError(f"advisor {info.name} 缺少必需属性 {attr}")
        mods.append(mod)
    return sorted(mods, key=lambda m: m.NAME)


def advise_all(video: str) -> dict[str, dict[str, Any]]:
    """跑全部 advisor，产出 decision["items"] 的内容（尚未拍板，chosen=None）。"""
    items: dict[str, dict[str, Any]] = {}
    for mod in discover():
        out = mod.advise(video)
        items[mod.NAME] = {
            "advisor": mod.__name__.rsplit(".", 1)[-1],
            "critical": bool(getattr(mod, "CRITICAL", False)),
            "flag": getattr(mod, "FLAG", None),
            "recommended": out.get("recommended"),
            "reason": out.get("reason", ""),
            "detail": out.get("detail", {}),
            "chosen": None,          # 等人签字
        }
    return items


def render_flags(items: dict[str, dict[str, Any]]) -> list[str]:
    """把已确认的决策项渲染成 Phase 1 的命令行参数。

    这是本方案的关键：**参数由决策单生成，而不是靠人记得传**。
    「确认了人声分离却忘了加 --separate-vocals」这类不一致从源头消除。
    """
    by_name = {m.NAME: m for m in discover()}
    out: list[str] = []
    for name, it in items.items():
        mod = by_name.get(name)
        chosen = it.get("chosen")
        if mod is not None and hasattr(mod, "render"):
            out.extend(mod.render(chosen))
        else:
            out.extend(_default_render(it.get("flag"), chosen))
    return out
