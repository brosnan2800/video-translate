# Spec 15 — 时间轴与静音修复（承载 issue #001）

## 目的
修复 issue #001 描述的两类现象：**cue 提前出现** 与 **cue 之间无空隙**。核心是"从源头保留真实停顿"，而非人为硬垫。

## 现象 A：cue 提前出现
- 根因：段级 `start` 含前导静音——VAD 的 `speech_pad_ms=200` 在段首补了静音，且 whisper 段边界偏前；于是 cue 在 `start` 弹出，但第一个词晚 0.1–0.5s 才发声。
- 修复：`generate` 改用**首词 `start` / 末词 `end`** 替代段级 `start/end`（需词级时间戳，V3 由 Spec 12 保证）。

## 现象 B：cue 之间无空隙
- 根因（经实证，见 `docs/V3-STATUS.md` 与 issue #001）：不是"音频密"，而是 **whisper 把真实停顿吞了**——
  - 相邻段 `end_i == start_{i+1}`，墙到墙零间隙；
  - 真实静音被埋在长段**内部**（以 `明星模仿秀.mp4` 实测：1.2s 静音位于 seg 22.21–28.66 内部，0.7s 静音位于 seg 8.62–13.55 内部）。
- 修复：词级时间戳 + stable-ts `split_by_gap` 在**静音处切开**长段，让字幕体现真实停顿（Spec 12 + 13 的词级数据支撑）。
- 实证样本：`明星模仿秀.mp4`（64.8s，含多处真实停顿）作为 golden 输入。

## `--gap` 兜底（非主要手段）
- `generate` 新增 `--gap`（默认 **0.2s**）：两遍钳制——`en_i = min(en_i, st_{i+1} - gap)` 且 `en_i >= st_i`。
- 语义：**只裁掉尾随静音、不让相邻 cue 重叠、绝不制造间隙**。仅当真实静音仍被吞时兜底，不依赖它"造空隙"。
- 退化路径：无 `words` 时不钳制或回退段级时间。

## 否决方案
- 方案 C（固定偏移）：会系统性破坏声学对齐，否决。

## 关联
- 测试：`tests/test_generate_golden.py`（`test_build_outputs_uses_word_boundaries`、`test_build_outputs_gap_clamp`、`test_build_outputs_fallback_no_words`）、`tests/test_timing_issue001.py`（明星模仿秀样本 + `ffprobe silencedetect` ground truth）。
- 决策：ADR-009。
