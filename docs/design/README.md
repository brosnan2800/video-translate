# 设计文档索引（中文）

本目录是 video-translate 的**原理级**设计文档，与 `specs/`（契约）、`adr/`（取舍）
构成三层互补。本文档回答"为什么这么设计、背后的机制是什么、V1→V2→V3 怎么演进"。

## 文档清单

- **[translation-design.md](translation-design.md)** —— 主文档
  - 技术架构总览（三阶段流水线、两条全局原则、词级时间轴如何贯穿）
  - 技术原理（词级对齐、静音被吞与复活、剪映 42 字行宽、CPU/int8 确定性）
  - **翻译断句的整体机制**（先 merge 再 split、句末标点、三阈值真义、词级断行、
    静音保持、失效模式、术语表参与）
  - 版本演进与各自的"为什么"（V1 / V2 / V3 能力对照表）
  - 决策依据与未决项
- **[references.md](references.md)** —— 附录：引用的外部项目思想
  - faster-whisper / stable-ts / WhisperX / deep-translator / agent 对话流
  - 每个项目"借了什么 / 没借什么 / 为什么"，统一收尾哲学

## 阅读建议

- 想理解"一条字幕是怎么一段段长出来的" → 直接读 `translation-design.md` 第 4 章。
- 想理解"为什么时间戳不准、又怎么修" → `translation-design.md` §3.1–§3.2、§4.5。
- 想理解"V3 为什么没用 stable-ts" → `translation-design.md` §6 + `adr/008-adopt-stable-ts.md`。
- 想理解"断行/静音/术语表为什么这么取舍" → 对应 `adr/009-*`、`adr/010-*`。
- 想看思想溯源 → `references.md`。

## 与其他文档的关系

```
docs/specs/   契约：行为应当如何（00–16）
docs/adr/     取舍：为什么选 A 不选 B（001–010）
docs/design/  原理：机制、思想、演进（本文档）
docs/V3-STATUS.md   V3 完成 / 未完成清单
```
