# ADR 039 — Display-layer cue merge belongs in the presentation layer

## 状态

Accepted（2026-09-14）

## 背景

`st.mp4` 末段字幕碎（排比式短句 + 0.7~2.7s 真实停顿）。需要把相邻的短显示块连排合并，
减少连闪。

## 决策

把"短块合并"实现为 **generate 阶段（表现层）的纯显示变换**，而不是转写层的段合并。

## 选项对比

### 选项 A：转写层合并（`merge_segments` 放宽 + 关 `respect_sentence_end`）

- 段数会变 → 必须**重译**（旧的 index 对不上，AGENTS.md 红线）。
- 合并后 cue 会盖住 ≥1s 停顿 → 声学层 cross-silence 反而**变多**（实测 12 项会更红）。
- 与"声学时间戳不变式"（ADR-009/012：时间戳是声学事实，不可重算）冲突：把停顿并进窗
  等于篡改了窗的声学含义。

### 选项 B（选定）：表现层显示合并（Spec 26）

- 只读 `segments` / `zh`，只在写 SRT 时把相邻短 cue 连排；`words` / `start` / `end` 一律不写。
- 与 `--tail` / `--min-dur` / `--gap` / `--offset` 同族，都是"显示窗后处理"，不触碰声学层。
- 不动段数 → **不重译**；声学层 14 项保持原样（它们本就非翻译缺陷）。
- 库默认关 → golden 字节级回归不受影响。

## 后果

- 正面：零成本修掉"碎"与"42 字符切出的断裂"（如 `for the other person. Forgiveness is you.`
  被切成的两条并回一条）。
- 风险：合并后单条字幕中英各最多 2 行（共 4 行），极长排比仍可能偏满——由侧车
  `<base>.display_merge.json` 完全可审计。
- 约束：SRT cue 数不再等于 segment index 数，verify 的 presentation 车道只报告
  （非闸门），内容/语义车道不受影响（它们读 segments+zh，不读 SRT）。

## 与既有不变式的关系

- ADR-012（acoustic timestamp truth）：选项 A 违反；选项 B 遵守（表现层窗变换从来允许）。
- ADR-035 M3（数据契约）：新增 `display_merge` artifact 单一登记，命名不散落。
