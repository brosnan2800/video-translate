# Spec 16 — 详细设计文档（登记）

## 目的
登记 V3 详细设计文档的存在与落点，并界定 **specs / ADR / design** 三者的分工，避免文档职责混乱。

## 三者分工
| 文档类型 | 定位 | 回答的问题 |
|---|---|---|
| `docs/specs/NN-*.md` | 契约 | 做什么、输入输出、不变量、可测契约 |
| `docs/adr/NNN-*.md` | 取舍 | 为什么这么选、放弃了什么 |
| `docs/design/*.md` | 原理与机制 | 知其所以然：技术原理、设计权衡、版本演进 |

## 落点
- `docs/design/translation-design.md`：**主文档**。技术架构、技术原理、**翻译断句的整体机制（核心）**、V1→V2→V3 演进与各自的"为什么"、决策依据。
- `docs/design/references.md`：**附录**。专讲引用的优质项目思想（借了什么 / 没借什么 / 为什么）——stable-ts、faster-whisper、WhisperX、deep-translator、OpenMontage。
- `docs/design/README.md`：中文索引，指向上面两份。

## 写作红线（用户强调）
**不写成"哪一段代码调了哪个接口"的接口说明书。** 聚焦原理、取舍、决策依据，用"为什么"贯穿每一节。尤其"翻译断句的整体机制"一章要讲清：为何先 merge 再 split、句末标点判定、gap/dur/max_chars 三阈值真义、词级 split_by_length、静音保持不重算时间戳。

## 关联
- 收尾交付物之一；与 V3 代码、测试、README 同批提交。
