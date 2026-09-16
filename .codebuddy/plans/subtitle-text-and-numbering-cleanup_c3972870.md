---
name: subtitle-text-and-numbering-cleanup
overview: 修复字幕文本内嵌换行（单行不变量 + 源头/边界清洗），并修正 Spec/ADR 编号撞号（asr-provider 25→27、display-merge 的 Spec 26 文档缺失），同时落地已确认的 verify 解耦（移除 Whisper 自证的低置信道）。
todos:
  - id: fix-spec-numbering
    content: 修正 Spec 编号：git mv 25-asr-provider.md 为 27-asr-provider.md，同步 7 处引用（含 asr.py/tests/index.md/ADR-038/MAJOR_VERSION_PLAN），补写缺失的 specs/26-display-merge.md；用 [subagent:code-explorer] 穷举"Spec 25"引用避免误伤 cli-path-hygiene
    status: completed
  - id: adr039-single-line
    content: 写 ADR-039（字幕文本单行不变量）+ 更新 specs/01-segment-schema.md 与 04-generate-srt.md；明确中英分行与 display-merge 折行不在改动范围
    status: completed
  - id: impl-single-line
    content: 新建 text_utils.py 的 to_single_line，并在 transcribe._seg_to_dict、translate 译文、generate.build_outputs 三处接入清洗（源头+边界双保险）
    status: completed
    dependencies:
      - adr039-single-line
  - id: test-single-line
    content: 新增 tests/test_single_line_text.py（含 \n 的事故几何回归，S3）并同步 AGENTS.md §3.2 翻译协议"zh 值不得含换行"；用 [skill:lsp-code-analysis] 核查调用链完整
    status: completed
    dependencies:
      - impl-single-line
  - id: adr040-verify-decouple
    content: 写 ADR-040（verify 与 Whisper 自证解耦）+ 更新 specs/18-verify.md（Lane 1 纯 FFmpeg）+ ADR-031 加 supersede 补遗标注
    status: completed
  - id: impl-verify-decouple
    content: 移除 verify.py 的 LOW_CONFIDENCE 与 find_low_confidence_segments、cli.py 的 import/调用/报告分支，改造 3 处测试并更新 AGENTS.md §2 护栏表；保留 _raw_indices 契约供 review 信号 A 使用
    status: completed
    dependencies:
      - adr040-verify-decouple
  - id: regression-commit
    content: 全量 uv run pytest 回归（基线 671 passed）+ 文档链接校验 + 分主题提交推送
    status: completed
    dependencies:
      - fix-spec-numbering
      - test-single-line
      - impl-verify-decouple
---

## 产品概述

video-translate 项目当前有三项待办工程治理，其中"字幕内嵌换行"是用户当前痛点。

## 核心功能

### 1. 字幕文本单行不变量（本次核心，用户痛点）

修复"单语 `zh.srt` / `en.srt` 里也有回车换行"的问题。经代码验证，根因是**字幕文本内部的换行符从未被任何环节清洗**，从源头透传到 SRT：

- `transcribe._seg_to_dict` 与 `translate.translate_one` 都只做 `.strip()`（只去首尾，不清中间）
- Agent 翻译产物 `zh_segments.json` 的 value 无任何约束（AGENTS.md §3.2 只要求"100% 覆盖 index"），是最可能的来源
- `generate.build_outputs` 拼装时把中间 `\n` 直接落进 SRT

目标：所有字幕文本字段（`segments_raw[].text` / `segments[].text` / zh 每个 value）**必须单行、不含 `\r\n`**。

**必须区分、不可误伤**：

- `bilingual.srt` 每条 cue 的"中文行 + 英文行"是 Spec 04 的**设计**，保留
- `--display-merge` 的显示层折行（`_wrap_zh` / `_wrap_en`，默认 OFF）是**有意的表现层行为**，保留

### 2. Spec 编号治理（修正我上一批的失误）

- `docs/specs/25-asr-provider.md`（新建）与已存在的 `docs/specs/25-cli-path-hygiene.md` **撞号**，应改为 **27**
- `docs/specs/26-*.md` **缺失**：`generate.py` 多处引用 "Spec 26: display-layer merge"，但文档从缺
- 编号全景：25 = cli-path-hygiene（存在）、26 = display-merge（代码有、文档无）、27 = asr-provider

### 3. verify 与 Whisper 自证解耦（方案已确认）

verify 里唯一越界的是 `find_low_confidence_segments`（用 `no_speech_prob>=0.6` / `avg_logprob<-1.0` 做"用 Whisper 验证 Whisper"的巡检，且参与 strict gate）。用户已确认：**彻底移除**低置信道；**不动**①层内部 `review` 信号 A（它是 A∩B 合取、B 为主判官，属①层自检）。

判据：**自证**（模型内部评分字段）不该出现在 verify；**客观/独立**（时间戳几何 + FFmpeg 外部参照）保留。

## 技术栈

沿用项目现有栈：Python 3.12 + uv（`uv run ...` 为唯一命令入口）、pytest、纯标准库 `re`。

## 实现方案

### 一、字幕文本单行化（ADR-039）

**核心：新增纯函数 `to_single_line`（`src/video_translate/text_utils.py`）**

```python
_LINEBREAK_RE = re.compile(r"[ \t]*[\r\n]+[ \t]*")

def to_single_line(text: str) -> str:
    """字幕文本单行化（ADR-039）：换行(含两侧空白) → 单空格，去首尾。
    只处理换行，不动正文其他空白；绝不触碰时间戳（ADR-012）。"""
    return _LINEBREAK_RE.sub(" ", text or "").strip()
```

- 连续换行（`\n\n`）压成**一个空格**：保留句子间隔、不粘连内容
- 吸收换行两侧空格/制表符：避免产生双空格
- 中英统一规则：英文防止单词粘连；中文得到"你好 世界"式单行，比直接删除更无损

**三处调用（源头 + 边界双保险，符合 ADR-035 D4 边界校验）**

| 位置 | 改动 | 理由 |
| --- | --- | --- |
| `transcribe.py::_seg_to_dict` | `"text": to_single_line(s.text)` | 保证落盘 `segments*.json` 干净 |
| `translate.py` Google 译文 | 译文落库前 `to_single_line` | 同上 |
| `generate.py::build_outputs` | `en` / `cn` 双向清洗 | **必须**：`zh_segments.json` 是可被 Agent/人工直接编辑的产物，绕过所有转写侧代码 |


**契约登记（不改字段形状，遵守 ADR-035 D3）**

- `artifacts.py` 的 `segments_raw` / `segments` / `zh` 条目：注释注明 text/zh 为单行文本
- `AGENTS.md` §3.2 翻译协议补充：**zh 值不得包含换行**（堵住最主要来源）
- `docs/specs/01-segment-schema.md` / `04-generate-srt.md`：写入单行不变量

### 二、Spec 编号治理

- `git mv docs/specs/25-asr-provider.md docs/specs/27-asr-provider.md`
- 同步引用 25→27（ASR 相关 7 处）：`MAJOR_VERSION_PLAN.md`×2、`tests/test_asr_provider.py`、`src/video_translate/asr.py`×2、`docs/index.md`、`docs/adr/038-*.md`
- **不动** `25-cli-path-hygiene` 一套（specs 文件、`test_cli_path_hygiene.py`、`cli.py` 5 处注释、`AGENTS.md`×2）
- 补写缺失的 `docs/specs/26-display-merge.md`（行为契约：显示层合并 + 折行参数 + 默认 OFF）

### 三、verify 解耦（ADR-040）

**唯一越界点**：`find_low_confidence_segments`

| 文件 | 改动 |
| --- | --- |
| `verify.py` | 删常量 `LOW_CONFIDENCE`（:30）+ 函数 `find_low_confidence_segments`（:237-283） |
| `cli.py` | 删 import（:40-41）、调用（:2032-2035）、报告分支（:2095-2097） |
| `AGENTS.md:83` | 护栏表删"verify 巡检段级置信度" |
| `docs/specs/18-verify.md` | Lane 1 明确为「只使用 FFmpeg 独立参照；模型自评字段不参与」（原文本就未写该检查，补一句把边界写死） |
| `docs/adr/031-*.md` | 加「补遗：D3 已被 ADR-040 supersede」标注（遵守 ADR 不可变惯例，不改正文） |


**保留项**（客观/独立，不属自证）：`find_adjacent_overlaps`（时间戳几何）、`cue_in_silence` / `find_uncovered_speech`（FFmpeg silencedetect）、`verify_presentation`、`classify_vocals_energy`。

**关键约束**：`CONFIDENCE_FIELDS` / `_raw_indices` / `raw_sources` 契约**不可动** —— ①层 `review` 信号 A 仍依赖（用户确认不动），故 `tests/test_pipeline_field_contract.py` 的 3 个用例只能**改造**（改为验证 review 信号 A 经 raw 回查复明），不能整体删除。

## 性能与可靠性

- `to_single_line` 为 O(n) 正则替换，纯函数、无 I/O，对每段文本调用一次，开销可忽略
- 三处调用均为"替换现有 `.strip()`"，不新增遍历、不改变流水线阶段数
- 全部改动不触碰时间戳与段数（ADR-012 不变量），产物契约形状零变化

## 架构影响

无新架构模式引入。三项改动分别落在「文本内容层」「文档契约层」「verify 验证层」，互不耦合，可分步独立回归。

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose: 在实现前后做影响面核查——编号修正需穷举"Spec 25"全部引用（区分 asr-provider 与 cli-path-hygiene）；verify 解耦需穷举 `find_low_confidence_segments` / `LOW_CONFIDENCE` 的全部调用与测试依赖
- Expected outcome: 输出精确的"文件:行号"改动清单，确保零遗漏、零误伤

### Skill

- **lsp-code-analysis**
- Purpose: 用语义级引用查找替代文本搜索，确认 `find_low_confidence_segments` 与 `to_single_line` 的调用点/引用链，尤其 `test_pipeline_field_contract.py` 中跨文件契约依赖
- Expected outcome: 给出准确的引用链与符号定义位置，支撑"改造而非删除"的判断