---
name: fill-gaps-recovery-prefix-collapse-fix
overview: 修复 fill_gaps recovery 因解码起点落在洞起点（半句处）触发 Whisper prefix collapse 而漏翻的问题。通过扩展 pad 回溯或语义边界对齐，让 recovery 窗口从“前一句完整开头”开始解码；同步新增 ADR、更新 HISTORY.md、补充代码注释与单元测试。
todos:
  - id: impact-analysis
    content: 使用 [subagent:code-explorer] 与 [lsp-code-analysis] 完成 recovery 路径影响分析
    status: completed
  - id: patch-fill-gaps
    content: 修改 fill_gaps.py 扩展 recovery 起点 pad 回溯策略并更新注释
    status: completed
    dependencies:
      - impact-analysis
  - id: unit-tests
    content: 新增 tests/test_fill_gaps_prefix_collapse.py 覆盖 hard-cut recovery 场景
    status: completed
    dependencies:
      - patch-fill-gaps
  - id: adr-036
    content: 新增 docs/adr/036-fill-gaps-prefix-collapse-recovery.md 记录设计决策
    status: completed
    dependencies:
      - patch-fill-gaps
  - id: history-update
    content: 更新 docs/HISTORY.md 记录本次修复条目
    status: completed
    dependencies:
      - adr-036
  - id: end-to-end-verify
    content: 端到端验证 W2/W3 完整恢复且未引入回声/幻觉
    status: completed
    dependencies:
      - unit-tests
---

## Product Overview

修复 `fill_gaps` recovery 的 prefix collapse 缺陷。主转写后出现的对白空洞（如 W2 821.44–832.74、W3 894.46–905.76），当前 recovery 从"洞起点"（常落在前一句半句处）开始解码，且 pad 仅 ±0.5s，导致 Whisper 发生 prefix collapse，只能掏出洞尾片段。需要让 recovery 起点能够前推到前一句完整开头，从而完整恢复被吞对白。

## Core Features

- 扩展 `_probe` / `_probe_long_hole` 的 pad 回溯策略，覆盖前一句完整边界
- 保持现有 `_is_recovered_hallucination` / `_dedupe_seams` / `_is_echo` 守卫不变
- 新增/更新单元测试覆盖 hard-cut recovery 场景
- 新增 ADR-036 记录设计决策；更新 `HISTORY.md` 与代码注释

## Tech Stack Selection

- Python 3.12，现有 faster-whisper pipeline
- 仅修改 `src/video_translate/fill_gaps.py`，不触碰 `transcribe.py`、控制平面、ADR-033/034/035 架构

## Implementation Approach

**方案**：扩展 `_PROBE_PADS` 为大跨度 pad 列表（例如 `(0.2, 0.0, 0.5, 2.0, 4.0, 6.0)`），让 `_probe` 自动尝试多起点；对 `_probe_long_hole` 的首个 sub-window 也允许前推到 `gs - max_pad`，避免长洞首片同样切在半句上。

**关键决策**：

- 不引入新的 VAD/语义分割逻辑来确定"前一句开头"，避免增加复杂度和 I/O；用多 pad 旋转复用现有 `_probe` scoring 机制。
- 保持 `_MULTI_PROBE_MIN_WINDOW = 4.0`：短洞仍只试小 pad，避免过度回溯。
- 大 pad 可能引入前句 echo，但既有 `_is_echo` + `_dedupe_seams` 已能处理重叠/重复片段。
- 覆盖率早停（`_PROBE_GOOD_COVERAGE = 0.6`）可限制额外 decode 次数。

**性能与可靠性**：

- 多 pad 最多增加 2–3 次 decode，W2/W3 场景额外成本 <5%。
- 失败安全：若所有 pad 均未改善 coverage，仍返回当前最优结果，不会恶化输出。
- 不改变 `fill_gaps(...)` 输入输出契约，下游 merge/translate/verify 无感知。

## Implementation Notes

- 将 `_PROBE_PADS` 的注释更新为解释大 pad 用途与 prefix collapse 场景。
- 提取 `_probe_pads_for_window(window)` 小函数，便于单测和 ADR 引用。
- `_probe_long_hole` 切片时，首个 sub-window 起点应使用 `max(0, gs - max_pad)`，保证长洞头部也能避开半句。
- 所有修改保持向后兼容；不删除现有 pad 值，仅追加更大回溯选项。

## Architecture Design

- 变更范围严格限定在 `fill_gaps.py` 内部 recovery 路径。
- 守卫层（`_is_recovered_hallucination`, `_dedupe_seams`, `_is_echo`）全部复用，不改动。
- `verify` 声学 lane、控制平面、主转写流程均不受影响。

## Directory Structure

```
project-root/
├── src/video_translate/
│   └── fill_gaps.py                         # [MODIFY] 扩展 recovery 起点 pad 策略 + 注释
├── docs/adr/
│   └── 036-fill-gaps-prefix-collapse-recovery.md  # [NEW] ADR 记录根因与修复决策
├── docs/
│   └── HISTORY.md                           # [MODIFY] 记录本次修复与影响
└── tests/
    └── test_fill_gaps_prefix_collapse.py    # [NEW] hard-cut recovery 单测（mock decode）
```

## Key Code Structures

```python
# _PROBE_PADS 将扩展为从小到大、覆盖"半句"到"前句开头"的 pad 序列
_PROBE_PADS: tuple[float, ...] = (0.2, 0.0, 0.5, 2.0, 4.0, 6.0)

def _probe_pads_for_window(window: float) -> tuple[float, ...]:
    """Return pads to try for a recovery window."""
    if window < _MULTI_PROBE_MIN_WINDOW:
        return _PROBE_PADS[:1]
    return _PROBE_PADS
```

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose: 对 `fill_gaps.py` 做影响分析，确认 `_probe` / `_probe_long_hole` / `_decode_once` 的所有调用点及与守卫函数的交互关系。
- Expected outcome: 输出调用链与风险点清单，确保补丁不破坏现有 recovery / resegment / verify 路径。

### Skill

- **lsp-code-analysis**
- Purpose: 通过 LSP 语义分析查看 `fill_gaps` 入口到 recovery 的调用链、类型签名与引用关系。
- Expected outcome: 精确定位需要修改的函数边界和对外接口，避免遗漏依赖。