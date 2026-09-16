# ADR-041 — verify 与 ASR 自证解耦（移除低置信道）

- **状态**：接受（已实现）
- **日期**：2026-09-16
- **关联**：ADR-012（声学真值 / 三 lane）、ADR-031（**本 ADR supersede 其 D3**）、ADR-038（ASR 层抽离：判据「裁判只看成品，不看选手内心」）、Spec 18（verify 契约）、Spec 27（ASRProvider）、`review.py`（①层自检）
- **落地**：`verify.py`、`cli.py`、`AGENTS.md`、`tests/test_verify_hardening.py`、`tests/test_verify_gate.py`、`tests/test_pipeline_field_contract.py`

## 背景

`verify` 的「声学 lane」名义上叫声学，实际混了两种性质完全不同的检查：

| 检查 | 真相来源 | 性质 |
|---|---|---|
| `cue_in_silence` / `cue_cross_silence` / `first_cue_early` / `find_uncovered_speech` | **FFmpeg silencedetect** | ✅ 独立参照 |
| `find_adjacent_overlaps` | **cue 时间戳几何** | ✅ 客观事实 |
| `classify_vocals_energy` | FFmpeg volumedetect | ✅ 独立参照 |
| **`find_low_confidence_segments`** | **`no_speech_prob` / `avg_logprob`** | ❌ **自证** |

`find_low_confidence_segments` 用 **ASR 模型自己的内部评分字段**去巡检 ASR 自己的产物，并**参与 strict gate**（会让 verify 以 exit 8 失败）。这是「用 Whisper 验证 Whisper」——不是独立验证。

它与两种**非自证**的 ASR 字段用法必须区分开：

- **`review.py` 的信号 A**（①层内部，塞在 `fill_gaps`）：`A ∩ B` 合取，**B（FFmpeg 能量参照）是主判官**。它回答的是"要不要重处理"，不是"成品合格吗"。**属①层自检，本 ADR 不动**。
- **`merge.py::_low_confidence`**（①层内部幻觉过滤）：提前删段，同属①层自检。

## 判据（本 ADR 立下的分界线）

| 性质 | 定义 | verify 该不该用 |
|---|---|---|
| **自证** | 用模型的**内部评分字段**（`no_speech_prob` / `avg_logprob` / `compression_ratio`） | ❌ 不该 |
| **客观 / 独立** | 用成品的**几何属性**（时间戳重叠）+ **外部参照**（FFmpeg 音频） | ✅ 该用 |

一句话：**verify 是裁判，只看选手交出来的成品，不看选手的内心活动。**

## 决策

### D1. 移除 `find_low_confidence_segments` 及其 gate 参与权

- 删 `verify.py::find_low_confidence_segments` 与常量 `LOW_CONFIDENCE`
- 删 `cli.py` 的 import / 调用（并入 `acoustic_issues`）/ 报告分支
- 声学 lane 自此为**纯 FFmpeg 独立验证**

该能力已被①层覆盖：幻觉过滤（提前删）+ review A∩B（重处理决策）。

### D2. 保留（客观 / 独立，不属自证）

`find_adjacent_overlaps`（时间戳几何）、`cue_in_silence` / `cue_cross_silence` /
`first_cue_early` / `find_uncovered_speech`（FFmpeg silencedetect）、
`verify_presentation`、`classify_vocals_energy`。

### D3. ①层内部不动

`review.py` 信号 A 与 `merge.py::_low_confidence` 保持原样——它们是**选手的赛前自检**，
位于① ASR 层内部，不裁决成品，且 B 为主判官。

### D4. 契约保留

`CONFIDENCE_FIELDS` / `_raw_indices` / `raw_sources`（ADR-035 Z2）**全部保留**——
①层 `review` 信号 A 仍依赖它们经 `_raw_indices` 回查 raw 源段置信度。
`tests/test_pipeline_field_contract.py` 的相关用例**改造**（改为验证 review 信号 A
经 raw 回查复明），**不得整体删除**。

### D5. 文档同步

- `Spec 18` Lane 1 补一句把边界写死：「只使用 FFmpeg 独立参照；模型自评字段不参与」
- `AGENTS.md` §2 护栏表删去「verify 声学 lane 巡检段级置信度」
- `ADR-031` 加「补遗：D3 已被 ADR-041 supersede」标注（ADR 正文不变，遵守不可变惯例）

## 理由

- **验证独立性**：裁判引用被测对象的内部字段 = 自证，换模型即失效，且无法定位问题归属。
- **职责边界**：①层负责"交出干净的字幕"（幻觉过滤 + review），verify 负责"用独立参照检验
  成品"。分工清晰后，出问题能立刻定位是"①层没拦干净"还是"verify 判错"。
- **可替换性**：呼应 ADR-038——verify 不依赖 ASR 模型字段，换 ASR 引擎时 verify 零改动。

## 后果

- **正面**：verify 声学 lane 100% 独立于 ASR 模型；换引擎零影响；职责边界可复述。
- **代价 / 必须接受的盲区**：`silencedetect` 只测能量，抓不到"**有能量但非语音**"的幻觉
  （笑声 / 音乐下的编造）。该场景**由①层幻觉过滤负责**（它用 ASR 字段，是最懂自己模型的一方）。
  verify 不再兜①层的漏——**这是分层的必然结果，也是它值钱的地方**：宁可职责清晰、定位明确，
  也不让裁判替选手擦屁股。
- **旧产物**：不回溯；下次跑 `verify` 即按新 lane 行为。

## 未来（不在本 ADR 范围）

- 若将来需要一个"事后巡检已生成字幕"的入口，应作为**独立的诊断命令**（且明确标注
  "模型自检、非独立、换模型即 inert"），而不是塞回 verify 的门禁。
