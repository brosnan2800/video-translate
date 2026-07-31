# ADR-008 — stable-ts 调研与回退（最终采用路线 A）

- 状态：已回退（Spike 结论：路线 A，不用 stable-ts）
- 日期：2026-07-26
- 关联：Spec 12（word-level-timestamps）；风险见计划第 4 节

## 背景
V2 转写阶段直接用 `faster_whisper.WhisperModel`，`model.transcribe` 未开 `word_timestamps`，`chunk_N.json` 只有 `{start,end,text}`。没有词级时间戳，V3 的断行（Spec 13）与时间轴/静音修复（Spec 15）都无从落地。

候选方案：
- **(A) faster-whisper + `word_timestamps=True` + 自写 split**：零新增依赖，但 `split_by_length` / `split_by_gap` / 更稳的静音抑制要自己实现一遍。
- **(B) 引入 stable-ts**：在 faster-whisper 之上封装，自带 regroup（`split_by_length` / `split_by_gap` / `split_by_punctuation`）、词级稳定对齐、更鲁棒的静音抑制。

## 决策
选 **(B)**（用户拍板"当然选 B"）。

理由：
- stable-ts 的 `regroup` 是这套 merge/split 逻辑的"上游完整版"，白拿成熟能力，胜过自写。
- 词级稳定对齐质量优于裸 faster-whisper，时间戳更准。
- 底层后端仍是 faster-whisper（large-v3 + int8 + CPU），与 V2 模型投入一致。

## 后果
- 依赖变重：stable-ts 会拉 `torch` / `Demucs`（Spike 用 `pipdeptree` 确认）。
- API 签名差异：`load_model` / `transcribe` / `words` 结构与 faster-whisper 不同，需适配 chunk 循环（Spec 12）。
- 为降低改动面，采用 `regroup=False`：**保留 V2 自写 `merge.py` 当唯一合并真相源**，stable-ts 仅供词级时间戳，不接管段落重组。

## 回退
若 Spike 证实 stable-ts 在 py3.13 装不上或代价不可接受，则回退方案 (A)：faster-whisper 开 `word_timestamps` + `merge.py` 内自写 `split_by_length` / `split_by_gap`（路线 A，零新增依赖）。已记入计划风险与 `docs/V3-STATUS.md`。

## 决策更新（Spike 结果，2026-07-26）
Spike **触发回退路线 A**：
- 本机 Python 3.13 无 stable-ts 2.x（带 `regroup` 的可用版）的 wheel；pip 回退到的 1.0.3 是损坏旧版（`Requires` 为空、`import` 即 `ModuleNotFoundError: No module named 'whisper'`、未拉 torch），与本项目 faster-whisper/int8 体系不兼容。
- 已卸载损坏的 stable-ts 1.0.3。验证 `faster_whisper.WhisperModel.transcribe` 支持 `word_timestamps=True`（返回 `Segment.words`）。
- **最终采用路线 A**：零新增依赖，词级时间戳由 faster-whisper 原生提供，split / 静音修复在 `merge.py` 自写（纯函数、可单测）。V3 目标（断行、静音保留、时间轴修复）完全达成，且更契合 ADR-001 的 CPU/int8 确定性哲学。
- 文档相应更新：Spec 12 改写路线 A；ADR-009/010 不变。
