# Spec 13 — 字幕断行（cue splitting）

## 目的
在 segment-merge 之后新增一道 **split pass**，按词级时间轴把超长 cue 切成符合剪映行宽的子 cue，解决 V2 遗留的"超长行难读"问题。

## 核心不变量
> **先 merge 成句，再 split 断行；顺序不可逆。**

- merge（V2）按"句末标点 + gap + dur"把碎片拼成**语义完整的句子**（语义优先）。
- split 只在成句之后，按**词级时间轴**把过长句子断开成**可读行**（可读性优先）。
- 若先 split 再 merge，会把一个长句的词重新拼回，断行失效。故 split 必须位于 merge 之后、写盘之前。

## 机制
- 对 merge 后的 segment，若 `len(text) > max_chars`（默认 42），按**有词 / 无词**两条路径切分：
  - **有词**（Whisper，Spec 12 保证）：
    - 用 stable-ts 的 `split_by_length`（或 `merge.py` 内自写的等效逻辑）在 **词边界** 切分——**绝不切在词中**。
    - 子 cue 的 `start/end` 取词真实边界（首词 `start` / 末词 `end`），**不重算**。
    - 子 cue 携带对应的 `words` 切片。
  - **无词**（接口型 ASR，ADR-042 / Spec 29）：`merge._split_wordless`。切点为
    **空白 → 标点 → 硬切**（同样不劈开单词），子 cue 时间戳按**字符数比例**分摊原
    `[start, end]`。这些时间戳本就不是声学真值（ADR-042 D3 已接受），分摊只需保持
    「单调、连续、与文本长度成比例」。子 cue **不携带 `words`**（不凭空造）。
- `max_chars=42` 语义是**行宽上限，不是成句条件**（澄清 `merge.py` 注释设计意图）。

## 配置与开关
- 默认**开启**。
- `--no-split`：关闭 split，退化为 V2 行为（golden 不变，向后兼容）。
- `--merge-max-chars N`：覆盖 42（`config.merge_max_chars` 已存在，缺 CLI 标志；env `VT_MERGE_MAX_CHARS`、TOML `[merge]` 已支持）。
- `max_chars` 是**软上限**：`merge_short_cues`（stage 4）会为可读性把过短的子 cue 并回左邻居
  （条件见 `merge.py` 注释：片段时长 < `min_dur`、间隙 < 1.0s、左邻居不以句末标点结尾），
  合并结果**可能再度超过 42**。词级与文本级路径**行为一致**，属既有设计取舍
  （可读性 > 行宽），最终呈现由 `generate` 的折行负责。

## 变更记录
- **V3 原文**：「退化路径：若 segment 无 `words`（理论上不会发生，因 Spec 12 已保证），
  回退为整段不拆。」—— 该前提在 **ADR-042 引入接口型 ASR 后失效**：captions 来源
  **本来就没有 `words[]`**，于是 >42 字符的长句原样进入最终字幕。实测（真实 Shorts
  24s 视频）合并后出现 95 字符 / 5.17s 的单条 cue；补文本级切分后该条变为
  `39 + 32 + 22` 三段，超限 cue 由 4 条降到 1 条。

## 关联
- 测试：`tests/test_merge.py`（`test_split_long_cue_by_words`、`test_no_split_keeps_whole`、
  `test_emit_carries_words`、无词路径四例：`test_wordless_long_cue_is_split_by_text` /
  `test_wordless_split_never_breaks_inside_a_word` /
  `test_wordless_split_timestamps_are_monotonic_and_span_preserving` /
  `test_wordless_short_cue_is_untouched`）、`tests/test_merge_golden.py`。
- 上游：Spec 12（words 来源）、Spec 29（接口型 ASR 无词来源）；下游：Spec 15（generate 用 words 边界）。
