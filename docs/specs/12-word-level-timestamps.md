# Spec 12 — 词级时间戳接入（faster-whisper 原生 word_timestamps）

> Spike 结论（2026-07-26）：原计划引入 stable-ts（路线 B），但本机 Python 3.13 无 stable-ts 2.x（带 `regroup` 的可用版）的 wheel，pip 回退到的 1.0.3 是损坏旧版（缺 `whisper` 依赖、未拉 torch），与本项目 faster-whisper/int8 体系不兼容。按 **ADR-008 预授权回退路线 A**：用 faster-whisper 原生 `word_timestamps=True` 取词级时间戳，**零新增依赖**，split / 静音修复在 `merge.py` 自写。V3 目标（断行、静音保留、时间轴修复）100% 不变。

## 目的
在转写阶段开启词级时间戳，为 V3 的断行（Spec 13）与时间轴/静音修复（Spec 15）提供数据基础，**不引入任何新依赖**。

## 方案（路线 A）
- `transcribe.py` 继续使用 `faster_whisper.WhisperModel`（V2 后端不变）。
- `model.transcribe(..., word_timestamps=True, vad_filter=True, vad_parameters=VAD_PARAMS, ...)`。
- faster-whisper 在 `word_timestamps=True` 时，返回的 `Segment` 带 `words: List[Word]`，每个 `Word` 有 `.word` / `.start` / `.end`（绝对秒）。
- 构造 segment dict 时，增加 `words: [{word, start, end}]`（绝对秒、2 位小数；跨 chunk 按 `cstart` 偏移校正，与段级时间一致）。
- 保持 V2 的 chunked + resumable：`chunk_N.json` 现含 `words`；resume 跳过已完成 chunk。
- 段级 `start/end` 仍保留（供 `--no-split` / 无 words 退化路径）。

## 自写 regroup（替代 stable-ts 的 split）
stable-ts 的 `split_by_length` / `split_by_gap` 本质是基于 `words` 的切分，本项目自写在 `merge.py`：
- `split_by_length(words, max_chars)`：按词顺序累加文本长度，超过 `max_chars` 时在**词边界**断开（绝不切词中）；子段 `start/end` 取词真实边界。
- `split_by_gap(words, max_gap)`：相邻词间隙 > `max_gap` 时断开（用于在真实静音处切分，Spec 15）。
这俩是纯函数、可单测，不依赖 torch。

## 契约（不跑模型即可测）
- `words` 非空且按 `start` 单调。
- `words[-1].end ≈ segment.end`（容差内）。
- 跨 chunk 偏移正确：`words` 时间 = 局部时间 + `cstart`。

## 风险（已消除）
- 原路线 B 的 torch/Demucs 重依赖、py3.13 兼容性 → 路线 A 完全规避（仅 faster-whisper + deep-translator，二者已装）。
- API 差异 → 无（沿用 V2 同一后端）。

## 关联
- 测试：`tests/test_transcribe_contract.py`（`test_transcribe_stores_words`，用 FakeModel 模拟 `.words`）。
- 下游：Spec 13（split 消费 words）、Spec 15（generate 消费 words）。
