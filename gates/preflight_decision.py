#!/usr/bin/env python3
"""preflight_decision.py — Phase 0→1 之间的人工确认闸门（决策单）。

要解决的问题
-----------
仓库现状：``doctor`` 能算出 VAD 路由与人声分离建议，但只 ``print`` 一句话，
**执行与否靠人/agent 自觉**；``--style`` 有默认值 film，会悄悄生效。于是：
  - 该开人声分离却忘了传 --separate-vocals → 转写建在坏地基上，只能重跑整条链
  - 风格没主动选过 → 翻完才发现轨错了
两者都属于「建议是软的、默认是静默的」，正是失控的源头。

本模块的四步机制
--------------
  1. propose  跑 advisors/ 下全部决策项 → 落盘 <base>.preflight_decision.json
  2. set      人工逐项拍板（chosen）
  3. confirm  校验全部 CRITICAL 项已拍板 → 签字（confirmed=true + 时间戳 + 视频指纹）
  4. assert   Phase 1 前置闸门：未签字 / 指纹不符 → 非零退出，物理上进不了 transcribe
     render-flags  把已签字的决策渲染成 Phase 1 命令行参数

设计要点：**参数由决策单渲染，不靠人记得传 flag** —— 不是"检测不一致"，
而是从源头消除不一致。签字过的东西一定会被执行。

视频指纹（防串单）
---------------
决策单记录视频的 size + mtime。换了素材或重新导出后再跑，指纹不符即报
STALE DECISION —— 避免拿上一个视频的决策单跑这一个。

退出码（与 checkpoint.py 对齐）
---------------------------
  0  OK
  2  PREREQUISITE VIOLATION  决策单不存在（没跑过 propose）
  3  GATE VIOLATION          未签字 / CRITICAL 项未拍板
  4  STALE DECISION          决策单与当前视频指纹不符
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from advisors import advise_all, render_flags  # noqa: E402

VIDEOS = Path("videos")
SCHEMA_VERSION = 1


def decision_path(base: str) -> Path:
    return VIDEOS / f"{base}.preflight_decision.json"


def _fingerprint(video: str) -> dict:
    p = Path(video)
    if not p.exists():
        return {"exists": False}
    st = p.stat()
    return {"exists": True, "size": st.st_size, "mtime": int(st.st_mtime)}


def _load(base: str) -> dict:
    path = decision_path(base)
    if not path.exists():
        print(f"[PREREQUISITE VIOLATION] 缺决策单 {path}\n"
              f"  → 先跑: make preflight VIDEO=videos/{base}.mp4", file=sys.stderr)
        sys.exit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def _save(base: str, dec: dict) -> None:
    path = decision_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dec, indent=2, ensure_ascii=False), encoding="utf-8")


def _coerce(raw: str):
    low = raw.strip().lower()
    if low in ("true", "yes", "y", "1", "on"):
        return True
    if low in ("false", "no", "n", "0", "off"):
        return False
    if low in ("none", "null", ""):
        return None
    return raw.strip()


# --- commands --------------------------------------------------------------

def cmd_propose(video: str, base: str) -> int:
    """跑全部 advisor，生成待签字的决策单。已存在时保留人已拍过的 chosen。"""
    old = {}
    if decision_path(base).exists():
        old = json.loads(decision_path(base).read_text(encoding="utf-8")).get("items", {})

    items = advise_all(video)
    for name, it in items.items():
        prev = old.get(name, {})
        # 保留人上次的拍板（重跑 propose 不该抹掉签过的决定）
        if prev.get("chosen") is not None:
            it["chosen"] = prev["chosen"]
        # 非 CRITICAL 且程序有建议 → 预填建议值（人仍可改）
        elif not it["critical"] and it["recommended"] is not None:
            it["chosen"] = it["recommended"]

    dec = {
        "schema_version": SCHEMA_VERSION,
        "base": base,
        "video": video,
        "video_fingerprint": _fingerprint(video),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "confirmed": False,
        "confirmed_at": None,
        "items": items,
    }
    _save(base, dec)
    print(f"[PROPOSE] 决策单已生成 -> {decision_path(base)}")
    _print_sheet(dec)
    return 0


def _print_sheet(dec: dict) -> None:
    print(f"\n{'='*72}\n决策单 base={dec['base']}  confirmed={dec['confirmed']}\n{'='*72}")
    for name, it in dec["items"].items():
        mark = "!" if it["critical"] else " "
        pend = "  <-- 待拍板" if it["chosen"] is None else ""
        print(f"[{mark}] {name}")
        print(f"      建议 : {it['recommended']}")
        print(f"      理由 : {it['reason']}")
        print(f"      已选 : {it['chosen']}{pend}")
    crit_pending = [n for n, it in dec["items"].items()
                    if it["critical"] and it["chosen"] is None]
    print(f"{'-'*72}")
    if crit_pending:
        print(f"待拍板的关键项: {crit_pending}")
        print(f"  → uv run python gates/preflight_decision.py set --base {dec['base']} "
              f"--item {crit_pending[0]} --value <值>")
    else:
        print(f"全部关键项已拍板，可签字:")
        print(f"  → uv run python gates/preflight_decision.py confirm --base {dec['base']}")
    print()


def cmd_show(base: str) -> int:
    _print_sheet(_load(base))
    return 0


def cmd_set(base: str, item: str, value: str) -> int:
    dec = _load(base)
    if item not in dec["items"]:
        print(f"[ERROR] 无此决策项 {item}，可选: {list(dec['items'])}", file=sys.stderr)
        return 1
    val = _coerce(value)
    # 用 advisor 的 render 做一次即时合法性校验（非法值当场炸，而不是留到跑命令时）
    try:
        render_flags({item: {**dec["items"][item], "chosen": val}})
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {item}={value!r} 非法: {e}", file=sys.stderr)
        return 1
    dec["items"][item]["chosen"] = val
    dec["confirmed"] = False           # 改了决策 → 签字自动失效，必须重签
    dec["confirmed_at"] = None
    _save(base, dec)
    print(f"[SET] {item} = {val!r}（签字已失效，需重新 confirm）")
    return 0


def cmd_confirm(base: str) -> int:
    dec = _load(base)
    pending = [n for n, it in dec["items"].items()
               if it["critical"] and it["chosen"] is None]
    if pending:
        print(f"[GATE VIOLATION] 关键决策项未拍板: {pending}，不许签字", file=sys.stderr)
        return 3
    dec["confirmed"] = True
    dec["confirmed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dec["confirmed_flags"] = render_flags(dec["items"])
    _save(base, dec)
    print(f"[CONFIRM] 已签字 base={base}")
    print(f"[CONFIRM] Phase 1 将使用参数: {' '.join(dec['confirmed_flags']) or '(无附加参数)'}")
    return 0


def cmd_assert(base: str, video: str | None) -> int:
    """Phase 1 前置闸门：未签字 / 指纹不符 → 非零退出，transcribe 进不去。"""
    dec = _load(base)
    if not dec.get("confirmed"):
        print(f"[GATE VIOLATION] Phase 0 决策单未签字，禁止进入 Phase 1\n"
              f"  → uv run python gates/preflight_decision.py show --base {base}",
              file=sys.stderr)
        return 3
    vid = video or dec.get("video")
    if vid:
        now = _fingerprint(vid)
        was = dec.get("video_fingerprint", {})
        if now.get("exists") and was.get("exists") and (
            now["size"] != was.get("size") or now["mtime"] != was.get("mtime")
        ):
            print(f"[STALE DECISION] 视频指纹与签字时不符（素材已变更），"
                  f"禁止沿用旧决策单\n  → 重跑 make preflight VIDEO={vid}", file=sys.stderr)
            return 4
    print(f"[OK] Phase 0 已签字于 {dec['confirmed_at']}，参数: "
          f"{' '.join(dec.get('confirmed_flags') or []) or '(无)'}")
    return 0


def cmd_render_flags(base: str) -> int:
    """输出 Phase 1 命令行参数（供 Makefile $(shell ...) 直接取用）。"""
    dec = _load(base)
    if not dec.get("confirmed"):
        print("[GATE VIOLATION] 未签字，不渲染参数", file=sys.stderr)
        return 3
    print(" ".join(dec.get("confirmed_flags") or render_flags(dec["items"])))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 0→1 人工确认闸门（决策单）")
    p.add_argument("cmd", choices=["propose", "show", "set", "confirm", "assert",
                                   "render-flags"])
    p.add_argument("--base", required=True, help="视频 base 名（不含扩展名）")
    p.add_argument("--video", default=None, help="视频路径（propose 必填）")
    p.add_argument("--item", default=None, help="决策项 key（set 用）")
    p.add_argument("--value", default=None, help="决策项取值（set 用）")
    a = p.parse_args()

    if a.cmd == "propose":
        if not a.video:
            print("[ERROR] propose 需要 --video", file=sys.stderr)
            return 1
        return cmd_propose(a.video, a.base)
    if a.cmd == "show":
        return cmd_show(a.base)
    if a.cmd == "set":
        if a.item is None or a.value is None:
            print("[ERROR] set 需要 --item 和 --value", file=sys.stderr)
            return 1
        return cmd_set(a.base, a.item, a.value)
    if a.cmd == "confirm":
        return cmd_confirm(a.base)
    if a.cmd == "assert":
        return cmd_assert(a.base, a.video)
    if a.cmd == "render-flags":
        return cmd_render_flags(a.base)
    return 1


if __name__ == "__main__":
    sys.exit(main())
