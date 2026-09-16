# ADR-040 — 字幕文本单行不变量（清除内嵌换行）

- **状态**：接受（实现中）
- **日期**：2026-09-16
- **关联**：ADR-035（数据契约总线）、ADR-012（只改文本、绝不重算时间轴）、ADR-039（display-layer merge，反向参照：显示层折行是有意的）、Spec 01（segment schema）、Spec 03（translate）、Spec 04（generate）
- **落地**：`src/video_translate/text_utils.py`、`transcribe.py`、`translate.py`、`generate.py`、`tests/test_single_line_text.py`

## 背景

用户报告：**单语 `zh.srt` / `en.srt` 里也有回车换行**。单语字幕本应"一条字幕一行"，断行应由用户在剪辑软件里自行处理。

根因：**字幕文本内部的换行符从未被任何环节清洗**，从源头一路透传到 SRT。

`srt_utils.block` 的换行完全由调用方传入的 `lines` 决定：

```python
return f"{index}\n{srt_time(start)} --> {srt_time(end)}\n" + "\n".join(lines) + "\n"
```

而文本进入这条链路时只做了 `.strip()`（**只去首尾空白，不清中间**）：

| 环节 | 现状 | 问题 |
|---|---|---|
| `transcribe._seg_to_dict` | `"text": s.text.strip()` | ASR 文本若含内嵌 `\n` 原样保留 |
| `translate.translate_one` | `text = text.strip()` | Google 译文若含 `\n` 原样保留 |
| Agent 翻译产物 `zh_segments.json` | 无任何约束（AGENTS.md §3.2 只要求 100% 覆盖 index） | **Agent 按句分行输出 `"你好\n世界"` 是最常见来源** |
| `generate.build_outputs` | `.strip()` 拼装 | 中间 `\n` 直接落进 SRT |

后果：一条 cue 内部出现 2+ 行文本，导入剪映后表现为"字幕自己换行"。

**必须区分、不可误伤的两种换行**：

- **`bilingual.srt` 的中英分行**（每条 cue 中文行 + 英文行）是 Spec 04 的**设计**；
- **`--display-merge` 的折行**（`_wrap_zh` / `_wrap_en`，ADR-039，默认 OFF）是有意的**显示层**行为。

本 ADR 只处理**文本内容自身混入的换行**。

## 决策

### D1. 单行不变量（single-line invariant）

**所有字幕文本字段必须是单行、不含 `\r` / `\n`：**

- `segments_raw[].text`、`segments[].text`（Spec 01）
- `zh_segments.json` 的每个 value（Spec 03）
- 恢复段 / resegment 段 / backfill 段的 text 同样适用

这是一条**数据契约级不变量**，不是某个 stage 的实现细节。cue 的"多行"只能来自 `block()` 的 `lines` 参数（中英分行 / 显示层折行），**不能来自文本内容**。

### D2. 清洗位置：源头 + 边界（双保险）

| 位置 | 理由 |
|---|---|
| **源头**：`transcribe._seg_to_dict` | 保证落盘的 `segments*.json` 本身就干净 |
| **源头**：`translate` 的 Google 译文 | 同上 |
| **边界**：`generate.build_outputs` | `segments_en.json` / `zh_segments.json` 是**可被人工 / Agent 编辑**的产物（翻译停点的直接产物），边界必须再兜一次（ADR-035 D4「边界校验」） |

只做源头是不够的：Agent 翻译产物绕过了所有转写侧代码。

### D3. 清洗规则：换行 → 单空格

```python
re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", text).strip()
```

- 连续换行（`\n\n`）压成**一个空格**（保留句子间隔，不粘连内容）
- 换行两侧的空格 / 制表符一并吸收，避免产生双空格
- **不影响其他空白**（不动正文中间已有的空格），也不改任何时间戳（ADR-012）

对中英文统一规则：英文需要空格以免单词粘连；中文得到"你好 世界"这样的单行，读起来仍是正常间隔，且比直接删除更无损。

### D4. 契约登记

- `artifacts.py` 的 `segments_raw` / `segments` / `zh` 条目：在注释中登记 `text` / `zh` 为**单行文本**（不改字段形状，遵守 ADR-035 D3 命名单一来源）。
- `AGENTS.md` §3.2 翻译协议补充：**生成的 `zh` 值不得包含换行**（换行由剪辑软件处理，不由译文携带）——这是堵住最主要来源的关键。
- Spec 01 / Spec 04 写入该不变量。

### D5. 不改的

- `bilingual.srt` 的中英分行（Spec 04 设计）
- `--display-merge` 的显示层折行（ADR-039，有意的表现层行为）
- 时间戳 / 断句 / 段数（本 ADR 只改文本内容，遵守 ADR-012 不变量）

## 理由

- **用户预期**：断行属于剪辑软件的表现层职责，程序不应把译文里的分行带进字幕。
- **职责边界**：字幕文本是"内容"，分行是"排版"。内容里的换行是脏数据，应在内容层清除。
- **代价极低**：一个纯函数 + 三处调用，无新依赖、无新产物、不改时间轴。
- **可回归**：事故几何（含 `\n` 的 zh / text）可直接固化为单测（S3）。

## 后果

- **正面**：单语字幕回归"一条一行"；双语 / 表现层的分行不受影响；文本字段有了明确的契约级不变量。
- **代价 / 注意**：
  - 若将来确实需要**文本内软换行**（例如超长句强制折行），必须走 `block()` 的 `lines`（表现层），而不是往文本里塞 `\n`——这一点已写进 Spec 04。
  - 旧产物（已生成的 SRT）不会自动修复；重跑 `generate` 即可（若 `segments_en.json` / `zh_segments.json` 也含换行，重跑时由边界清洗兜住）。

## 未来（不在本 ADR 范围）

- `verify` 内容层增加"文本内含换行"的巡检（当前 `validate_zh` 只查覆盖率）。
- 翻译停点的 task 协议（AGENTS.md §3.2）在 task 文件里显式提示"单行输出"。
