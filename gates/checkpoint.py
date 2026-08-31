#!/usr/bin/env python3
"""checkpoint.py — video-translate 代码层强制闸门（checkpoint 风格，对齐 OpenMontage）。

把"软红线 + 阶段顺序"从文档/建议升级成 物理上绕不过的代码强制：
  - 阶段 prerequisites：前驱未完成，后继阶段直接 PREREQUISITE VIOLATION，非零退出
  - 产物 schema 校验：每阶段 JSON 产物按 schema 校验，不合法不许标记 completed
  - gated 审批：标 human_approval_default 的阶段（translate 中文质量、verify 最终交付），
    未带 human_approved 就标记完成 -> GATE VIOLATION
  - verify 阶段额外读 .vt_verify_gate.json（Gap B 硬闸门状态）：breached 且未审批 -> 硬停

退出码约定（与仓库一致，6 = AWAITING_AGENT 保留）：
  0  OK
  1  GENERIC_FAIL
  2  PREREQUISITE_VIOLATION
  3  GATE_VIOLATION（gated 阶段未审批 / verify 未过闸）
  4  SCHEMA_VIOLATION
  6  AWAITING_AGENT（转写挂起，非错误）

注：本文件按本地仓库真实产物布局校准（segments_en.json 为列表结构、generate 产物
支持 _vN 版本化与嵌套目录）。ARTIFACTS 的路径/字段需与本地实际输出对齐。
"""
import argparse
import glob
import json
import sys
from pathlib import Path

CKPT = ".vt_checkpoint.json"
VIDEOS = Path("videos")

# Gap B verify 状态文件（由 gates/verify_gate.py 写出，complete verify 时消费）
GATE_FILE = "{base}.vt_verify_gate.json"

# 阶段图：stage -> 前驱（prerequisites）
PIPELINE = {
    "preflight":  [],
    "transcribe": ["preflight"],
    "translate":  ["transcribe"],
    "generate":   ["translate"],
    "verify":     ["generate"],
}
# 需人工审批的阶段（对应 AGENTS.md"中文质量只在末道 gate 审"）
GATED = {
    "translate": "中文质量需人工确认覆盖/对齐",
    "verify":     "最终交付需人工 gate（先 verify_gate run 写状态，breached 需 approve）",
}
# 每阶段产物 + 最小 schema（required 字段名）。
# 本地 segments_en.json 是 list[dict]（每段 text/start/end/words），无顶层 index，
# 故 transcribe 只校验"文件存在 + 是 list"；覆盖/索引校验交由 gate.py content 负责。
# generate 产物支持 _vN 版本号与嵌套目录，validate 时按 glob 兜底。
ARTIFACTS = {
    "transcribe": {"file": "{base}.segments_en.json", "required": []},
    "translate":  {"file": "{base}.zh_segments.json",  "required": []},
    "generate":   {"file": "{base}/{base}.bilingual.srt", "required": []},
}


def load() -> dict:
    return json.loads(Path(CKPT).read_text(encoding="utf-8")) if Path(CKPT).exists() else {}


def save(ck: dict) -> None:
    Path(CKPT).write_text(json.dumps(ck, indent=2, ensure_ascii=False), encoding="utf-8")


def status_of(ck: dict, stage: str) -> str:
    return ck.get("stages", {}).get(stage, {}).get("status", "pending")


def enforce_prerequisites(ck: dict, stage: str) -> None:
    for pre in PIPELINE[stage]:
        if status_of(ck, pre) != "completed":
            print(f"[PREREQUISITE VIOLATION] {stage} 的前驱 {pre} 未完成 "
                  f"(当前={status_of(ck, pre)})，禁止进入", file=sys.stderr)
            sys.exit(2)


def _artifact_ok(stage: str, base: str) -> bool:
    """产物是否存在（generate 支持 _vN 版本号 + 嵌套目录兜底）。"""
    spec = ARTIFACTS.get(stage)
    if not spec:
        return True
    rel = spec["file"].format(base=base)
    if (VIDEOS / rel).exists():
        return True
    if stage == "generate":
        for pat in (str(VIDEOS / f"{base}" / "*.bilingual.srt"),
                    str(VIDEOS / f"{base}_v*.bilingual.srt")):
            if glob.glob(pat):
                return True
    return False


def validate_artifact(stage: str, base: str) -> None:
    if stage not in ARTIFACTS:
        return
    if not _artifact_ok(stage, base):
        print(f"[SCHEMA] 缺产物 {VIDEOS / ARTIFACTS[stage]['file'].format(base=base)}",
              file=sys.stderr)
        sys.exit(4)


def _verify_gate_allows(base: str) -> bool:
    """Gap B：读取 .vt_verify_gate.json，决定是否允许标记 verify completed。

    - 文件不存在：视为未过闸（必须先跑 verify_gate run）。
    - breached=false：可交付。
    - breached=true：必须已 human_approved 才放行（半自动人工确认）。
    """
    p = VIDEOS / GATE_FILE.format(base=base)
    if not p.exists():
        return False
    try:
        g = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if g.get("breached"):
        ck = load()
        return bool(ck.get("stages", {}).get("verify", {}).get("human_approved"))
    return True


def check(stage: str, base: str) -> None:
    ck = load()
    enforce_prerequisites(ck, stage)
    validate_artifact(stage, base)
    if stage in GATED and not ck.get("stages", {}).get(stage, {}).get("human_approved"):
        print(f"[GATE] {stage} 需人工审批：{GATED[stage]}（先跑 approve {stage}）",
              file=sys.stderr)
        sys.exit(3)
    if stage == "verify" and not _verify_gate_allows(base):
        print(f"[GATE] verify 未过闸：先跑 verify_gate run，breached 需 approve verify",
              file=sys.stderr)
        sys.exit(3)
    print(f"[OK] {stage} 前置/产物/闸门通过")
    sys.exit(0)


def complete(stage: str, base: str) -> None:
    ck = load()
    enforce_prerequisites(ck, stage)
    validate_artifact(stage, base)
    st = ck.setdefault("stages", {}).setdefault(stage, {})
    # verify 的 gated 由下方 gate 状态把关（breached 才需审批），不走通用 always-approve
    if stage in GATED and stage != "verify" and not st.get("human_approved"):
        print(f"[GATE VIOLATION] {stage} 是 gated 阶段，未审批不得标记 completed",
              file=sys.stderr)
        sys.exit(3)
    if stage == "verify" and not _verify_gate_allows(base):
        print(f"[GATE VIOLATION] verify_gate 判定 breached 且未人工放行，禁止标记完成",
              file=sys.stderr)
        sys.exit(3)
    st["status"] = "completed"
    save(ck)
    print(f"[CKPT] {stage} -> completed")


def approve(stage: str) -> None:
    ck = load()
    ck.setdefault("stages", {}).setdefault(stage, {})["human_approved"] = True
    save(ck)
    print(f"[CKPT] {stage} human_approved=True")


def main() -> None:
    p = argparse.ArgumentParser(description="video-translate 代码层强制闸门")
    p.add_argument("cmd", choices=["status", "check", "complete", "approve"])
    p.add_argument("--stage", default="")
    p.add_argument("--base", default="")
    a = p.parse_args()

    if a.cmd == "status":
        print(json.dumps(load().get("stages", {}), indent=2, ensure_ascii=False))
        return
    if a.cmd == "approve":
        if not a.stage:
            print("--stage required", file=sys.stderr)
            sys.exit(1)
        approve(a.stage)
        return
    if not a.stage:
        print("--stage required", file=sys.stderr)
        sys.exit(1)
    if a.cmd == "check":
        check(a.stage, a.base)
    elif a.cmd == "complete":
        complete(a.stage, a.base)


if __name__ == "__main__":
    main()
