# Spec 13 — 字幕断行（cue splitting）

## 目的
在 segment-merge 之后新增一道 **split pass**，按词级时间轴把超长 cue 切成符合剪映行宽的子 cue，解决 V2 遗留的"超长行难读"问题。

## 核心不变量
> **先 merge 成句，再 split 断行；顺序不可逆。**

- merge（V2）按"句末标点 + gap + dur"把碎片拼成**语义完整的句子**（语义优先）。
- split 只在成句之后，按**词级时间轴**把过长句子断开成**可读行**（可读性优先）。
- 若先 split 再 merge，会把一个长句的词重新拼回，断行失效。故 split 必须位于 merge 之后、写盘之前。

## 机制
- 对 merge 后的 segment，若 `len(text) > max_chars`（默认 42）且带 `words`：
  - 用 stable-ts 的 `split_by_length`（或 `merge.py` 内自写的等效逻辑）在 **词边界** 切分——**绝不切在词中**。
  - 子 cue 的 `start/end` 取词真实边界（首词 `start` / 末词 `end`），**不重算**。
  - 子 cue 携带对应的 `words` 切片。
- `max_chars=42` 语义是**行宽上限，不是成句条件**（澄清 `merge.py` 注释设计意图）。

## 配置与开关
- 默认**开启**。
- `--no-split`：关闭 split，退化为 V2 行为（golden 不变，向后兼容）。
- `--merge-max-chars N`：覆盖 42（`config.merge_max_chars` 已存在，缺 CLI 标志；env `VT_MERGE_MAX_CHARS`、TOML `[merge]` 已支持）。
- 退化路径：若 segment 无 `words`（理论上不会发生，因 Spec 12 已保证），回退为整段不拆。

## 关联
- 测试：`tests/test_merge.py`（`test_split_long_cue_by_words`、`test_no_split_keeps_whole`、`test_emit_carries_words`）、`tests/test_merge_golden.py`。
- 上游：Spec 12（words 来源）；下游：Spec 15（generate 用 words 边界）。
