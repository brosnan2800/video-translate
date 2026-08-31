#!/usr/bin/env python3
"""gate.py — video-translate 三维硬闸门 + 阶段顺序守卫。

把 AGENTS.md 里的"软红线"变成可执行、可失败的硬闸门：
  - content  : 翻译 100% 覆盖 index（R4）+ 对齐后已重译（R2）—— 进 generate 前的闸门
  - acoustic / presentation : 调真实的 `uv run video-translate verify` 对应 Lane（R1 / R5）
  - all      : 三维 + 阶段顺序一起跑
  - stage-order: 漏步即红（缺 segments_en.json / zh_segments.json 之一就拦下）

任何子命令非零退出 = 闸门拦下，禁止进入下一阶段。
直接接到 Makefile / pre-commit / CI 即可。

用法（仓库根执行）:
  uv run python gates/gate.py content      --base <base>
  uv run python gates/gate.py acoustic     --base <base> --video <video.mp4>
  uv run python gates/gate.py presentation --base <base> --video <video.mp4>
  uv run python gates/gate.py all         --base <base> --video <video.mp4>
  uv run python gates/gate.py stage-order --base <base>
"""
import argparse
import os
import subprocess
import sys

# ---- 与 AGENTS.md / Spec 00 对齐的配置 ----
SEG = lambda base: f"videos/{base}.segments_en.json"
ZH = lambda base: f"videos/{base}.zh_segments.json"


def _run(cmd):
    return subprocess.run(cmd, cwd=os.getcwd())


def _verify(base, video):
    """调用真实的 verify 子命令（仓库 verify 已含声学/内容/表现三 Lane）。"""
    cmd = ["uv", "run", "video-translate", "verify",
           "--segments", SEG(base), "--zh", ZH(base)]
    if video:
        cmd += ["--video", f"videos/{video}"]
    return _run(cmd)


def content_gate(base):
    """R4：翻译 100% 覆盖 index；R2：对齐后已重译（段数可能变化）。"""
    zh = ZH(base)
    if not os.path.exists(zh):
        print("[CONTENT] FAIL: 缺 zh_segments.json（Stage 2 未完成）", file=sys.stderr)
        return 1
    # 调仓库自带的 validate_zh 做覆盖校验
    r = _run([
        "uv", "run", "python", "-c",
        f"from video_translate.translate import validate_zh; "
        f"import sys; sys.exit(0 if validate_zh('{SEG(base)}','{zh}') else 1)"
    ])
    if r.returncode != 0:
        print("[CONTENT] FAIL: 翻译未 100% 覆盖 index（漏行/合并行/非数字 key），"
              "或对齐后未重译", file=sys.stderr)
        return 1
    print("[CONTENT] PASS: 覆盖 100% + 索引对齐")
    return 0


def stage_order(base):
    """漏步即红：阶段产物必须按序存在。"""
    steps = [
        (SEG(base), "Transcribe 未完成（缺 segments_en.json）"),
        (ZH(base), "Translate 未完成（缺 zh_segments.json）"),
    ]
    ok = True
    for path, msg in steps:
        if not os.path.exists(path):
            print(f"[STAGE-ORDER] FAIL: {msg}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="video-translate 硬闸门")
    p.add_argument("lane", choices=["acoustic", "content", "presentation", "all", "stage-order"])
    p.add_argument("--base", required=True)
    p.add_argument("--video", default="")
    a = p.parse_args()

    if a.lane == "content":
        sys.exit(content_gate(a.base))
    if a.lane == "stage-order":
        sys.exit(stage_order(a.base))
    if a.lane == "all":
        rc = content_gate(a.base) or _verify(a.base, a.video).returncode or stage_order(a.base)
        sys.exit(rc)
    # acoustic / presentation：走真实 verify（仓库已含三 Lane）
    sys.exit(_verify(a.base, a.video).returncode)


if __name__ == "__main__":
    main()
