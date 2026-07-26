# ADR-010 — 术语表柔性注入 persona

- 状态：接受
- 日期：2026-07-26
- 关联：Spec 14

## 背景
多集 / 系列视频需要译名一致（角色名、专有名词）。同一名字在不同片段可能被翻成不同中文，破坏观感。候选：
- **(A) 强制词典替换**：翻译前把原文术语直接替换成指定中文。
- **(B) 注入 persona 上下文**：把"建议译名表"放进翻译任务，让 agent / Google 参考，但不强制。

## 决策
选 **(B)**。

理由：
- 强制替换（A）会破坏 V2 确立的"信达雅 + 口语感"——术语的上下文译法（如谐音梗、双关）被一刀切，翻译失去灵性。
- 柔性注入保留翻译的语境判断：persona 里写"以下术语建议译为 X"，agent 在绝大多数情况会采用，偶尔依语境调整，整体一致又不死板。
- 这与 V2 的"agent-as-engine"（persona 驱动）哲学一致：把约束放进 persona，而非硬编码替换逻辑。

## 后果
- 新增 `glossary.py`：`load_glossary(path) -> str | None`。
- `prepare_translate_task` 的 task JSON 增加 `glossary` 字段，拼入 persona 上下文。
- 配置仅显式提供（CLI `--glossary` / env `VT_GLOSSARY` / TOML `[translate] glossary`），不做项目级默认路径，避免隐式行为。
