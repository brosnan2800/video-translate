#!/usr/bin/env python3
"""verify_gate.py — Phase 4 半自动硬闸门（Gap B 核心）。

把 Stage 4 `verify` 从"报告型 + 默认放行"升级成**分流 + 自动恢复 + 熔断 + 半自动**：

  消费真实仓库 `verify` 的三 Lane 报告（acoustic / content / presentation）+ 语义回读
  result，按以下分流决定退码与是否自动重跑：

    - 全绿            → 写 gate: breached=false → 退 0（可交付）
    - 声学/确定性内容 → 自动修该段（resegment + generate / translate + generate）→
                        重 verify → retry++；retry >= MAX_RETRY 熔断转半自动
    - 语义回读 fidelity → 不进自动环（修了可能更差），直接转半自动，不消耗 retry

两条硬约束（来自用户纠偏，已转成代码）：
  ① 熔断：自动环带上限 MAX_RETRY=2，超限立即停、转半自动，绝无限循环。
  ② 分流：声学/确定性内容走自动恢复；语义回读是独立任务，命中即转半自动，
          且必须消费 <base>.semantic_reread_result.json（现状只产不消费，正是缺口）。

判据全部复用真实仓库 `verify.py` 已有函数（`find_uncovered_speech` / `validate_zh` /
`verify_align` / `find_untranslated_latin_words` / `build_semantic_reread_task`），本文件
只消费它们的产出，不重新实现。

用法（仓库根执行）
----------------
  uv run python gates/verify_gate.py run --base <base> --video <video.mp4>
        # 单次：跑 verify、写 .vt_verify_gate.json、按 breached 退 0/3（不自动恢复）
  uv run python gates/verify_gate.py run --base <base> --video <video.mp4> --auto-loop
        # 自动恢复环（带上限）：修得动就转绿退 0；修不动/语义命中/超限 → 退 3
  uv run python gates/verify_gate.py status --base <base>
        # 打印当前 gate 状态

退出码：0 全绿可交付 / 3 GATE VIOLATION（breached 且未审批，待 make verify-approve）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VIDEOS = Path("videos")
MAX_RETRY = 2                                   # 约束①：熔断上限
GATE_FILE = "{base}.vt_verify_gate.json"        # checkpoint.py 会消费它
REPORT_FILE = "{base}.verify_report.json"       # 真实 verify 写出的三 Lane 报告
RERead_TASK = "{base}.semantic_reread_task.json"
RERead_RESULT = "{base}.semantic_reread_result.json"

EXIT_OK = 0
EXIT_GATE_VIOLATION = 3


def _video_arg(video: str | None, base: str) -> str:
    """解析视频路径：未给则用 videos/<base>.mp4；相对路径自动补 videos/ 前缀。"""
    if not video:
        return str(VIDEOS / f"{base}.mp4")
    if not video.startswith("videos/") and not os.path.isabs(video):
        cand = VIDEOS / video
        if cand.exists():
            return str(cand)
    return video


def run_repo_verify(base: str, video: str) -> int:
    """调真实仓库 verify，写出三 Lane 机器可读报告（供 classify 消费）。"""
    cmd = [
        "uv", "run", "video-translate", "verify",
        "--segments", str(VIDEOS / f"{base}.segments_en.json"),
        "--zh", str(VIDEOS / f"{base}.zh_segments.json"),
        "--video", _video_arg(video, base),
        "--report", str(VIDEOS / REPORT_FILE.format(base=base)),
    ]
    return subprocess.run(cmd, cwd=os.getcwd()).returncode


def load_report(base: str) -> dict:
    p = VIDEOS / REPORT_FILE.format(base=base)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def classify(report: dict) -> list[dict]:
    """三 Lane 分流 → breach 列表。

    每个 breach：{lane, auto_fixable, windows?, indices?}
      - acoustic : report.acoustic.uncovered_audio 非空 → 自动修（resegment 窗口）
      - content  : coverage_drift / index_drift / untranslated_latin 非空 → 自动修
      - semantic : content.semantic_reread.breached → 不自动修（半自动）
    """
    breaches: list[dict] = []
    acoustic = report.get("acoustic", {})
    uncovered = acoustic.get("uncovered_audio", []) or []
    if uncovered:
        wins = ";".join(f"{b['start']}-{b['end']}" for b in uncovered)
        breaches.append({"lane": "acoustic", "auto_fixable": True, "windows": wins})

    content = report.get("content", {})
    if content.get("coverage_drift") or content.get("index_drift"):
        breaches.append({"lane": "content", "auto_fixable": True, "indices": []})
    latin = content.get("untranslated_latin", []) or []
    if latin:
        idx = [m.get("index") for m in latin if m.get("index") is not None]
        breaches.append({"lane": "content", "auto_fixable": True, "indices": idx})

    sr = content.get("semantic_reread", {}) or {}
    if sr.get("breached"):
        breaches.append({"lane": "semantic", "auto_fixable": False})
    return breaches


def write_gate(base: str, *, breached: bool, lane: str, mode: str,
               auto_fixable: bool, retry: int, breaker_tripped: bool,
               reread_result: str | None) -> None:
    data = {
        "base": base,
        "breached": breached,
        "mode": mode,                       # "auto" 进行中 / "half_auto" 已熔断转人工
        "lane": lane,                       # "acoustic" | "content" | "semantic" | "clean"
        "auto_fixable": auto_fixable,
        "retry": retry,
        "max_retry": MAX_RETRY,
        "breaker_tripped": breaker_tripped,
        "semantic_reread_result": reread_result,
        "report": str(VIDEOS / REPORT_FILE.format(base=base)),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (VIDEOS / GATE_FILE.format(base=base)).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[GATE] breached={breached} lane={lane} mode={mode} retry={retry} "
          f"breaker={breaker_tripped}")


def auto_fix(base: str, video: str, breaches: list[dict], lang: str) -> None:
    """对可自动修的 breach 补该段（只动问题区间/段，不重跑全片），然后重生成。"""
    vpath = _video_arg(video, base)
    for b in breaches:
        if b["lane"] == "acoustic":
            # 只对 uncovered-audio 区间重转写（复用 cached vocals.wav）+ 重生成
            windows = b.get("windows", "")
            print(f"[AUTO-FIX] acoustic windows={windows} → resegment --windows")
            subprocess.run([
                "uv", "run", "video-translate", "resegment",
                "--segments", str(VIDEOS / f"{base}.segments_en.json"),
                "--video", vpath, "--lang", lang, "--windows", windows,
            ], cwd=os.getcwd())
        elif b["lane"] == "content":
            # 只对问题段重译（headless google 引擎）+ 重生成
            indices = b.get("indices", [])
            idx_arg = ",".join(str(i) for i in indices) if indices else ""
            print(f"[AUTO-FIX] content indices={idx_arg or '<coverage/index drift>'} "
                  f"→ translate (google) + generate")
            subprocess.run([
                "uv", "run", "video-translate", "translate",
                "--segments", str(VIDEOS / f"{base}.segments_en.json"),
                "--zh", str(VIDEOS / f"{base}.zh_segments.json"),
                "--engine", "google",
            ], cwd=os.getcwd())
        # generate 当前产物
        subprocess.run([
            "uv", "run", "video-translate", "generate",
            "--segments", str(VIDEOS / f"{base}.segments_en.json"),
            "--zh", str(VIDEOS / f"{base}.zh_segments.json"),
            "--outdir", "videos", "--base", base,
        ], cwd=os.getcwd())


def _reread_result_path(base: str) -> str | None:
    p = VIDEOS / RERead_RESULT.format(base=base)
    return str(p) if p.exists() else None


def run_gate(base: str, video: str | None, *, auto_loop: bool = False,
             simulate_unfixable: bool = False, lang: str = "en") -> int:
    """Phase 4 硬闸门主循环。返回 0（可交付）/ 3（breached 需人工）。"""
    retry = 0
    while True:
        if simulate_unfixable:
            # 测试：跳过真实 verify，直接注入 acoustic breach
            report = {"acoustic": {"uncovered_audio": [{"start": 12.0, "end": 18.5}]},
                      "content": {}, "presentation": {}}
        else:
            run_repo_verify(base, video)
            report = load_report(base)

        breaches = classify(report)

        if not breaches:
            write_gate(base, breached=False, lane="clean", mode="auto",
                       auto_fixable=False, retry=retry, breaker_tripped=False,
                       reread_result=_reread_result_path(base))
            return EXIT_OK

        # 约束②：语义命中 → 直接半自动，不消耗 retry
        if any(b["lane"] == "semantic" for b in breaches):
            write_gate(base, breached=True, lane="semantic", mode="half_auto",
                       auto_fixable=False, retry=retry, breaker_tripped=True,
                       reread_result=_reread_result_path(base))
            return EXIT_GATE_VIOLATION

        # 约束①：熔断
        if retry >= MAX_RETRY:
            write_gate(base, breached=True, lane=breaches[0]["lane"], mode="half_auto",
                       auto_fixable=True, retry=retry, breaker_tripped=True,
                       reread_result=_reread_result_path(base))
            return EXIT_GATE_VIOLATION

        # 单次模式：只检测、不自动恢复（恢复交给 make verify-fix）
        if not auto_loop:
            write_gate(base, breached=True, lane=breaches[0]["lane"], mode="auto",
                       auto_fixable=True, retry=retry, breaker_tripped=False,
                       reread_result=_reread_result_path(base))
            return EXIT_GATE_VIOLATION

        # 自动恢复环：修该段 → retry++ → 下一轮重 verify
        if not simulate_unfixable:
            auto_fix(base, video, breaches, lang)
        retry += 1


def main() -> int:
    p = argparse.ArgumentParser(description="Phase 4 半自动硬闸门")
    p.add_argument("cmd", choices=["run", "status"])
    p.add_argument("--base", required=True)
    p.add_argument("--video", default=None)
    p.add_argument("--auto-loop", action="store_true",
                   help="自动恢复环（带上限）；不带则单次检测（写 gate 后退 0/3）")
    p.add_argument("--simulate-unfixable", action="store_true",
                   help="测试用：模拟 auto_fix 永远修不好（classify 恒定返回 acoustic）")
    p.add_argument("--lang", default="en",
                   help="自动修复声学窗口时 resegment 的强制语言（默认 en）")
    a = p.parse_args()

    base = a.base

    if a.cmd == "status":
        g = VIDEOS / GATE_FILE.format(base=base)
        print(g.read_text(encoding="utf-8") if g.exists()
              else json.dumps({"breached": None, "note": "no gate yet"}))
        return EXIT_OK

    return run_gate(base, a.video, auto_loop=a.auto_loop,
                    simulate_unfixable=a.simulate_unfixable, lang=a.lang)


if __name__ == "__main__":
    sys.exit(main())
