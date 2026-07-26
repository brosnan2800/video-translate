# 引用的外部项目思想（附录）

> 本章专讲：本项目从哪些优秀开源项目里"借了什么、没借什么、为什么"。
> 正文的原理与机制见 `translation-design.md`。这里只做思想溯源，便于维护者
> 理解"为什么长这样"，以及将来要升级该往哪看。

---

## 1. faster-whisper (`SYSTRAN/faster-whisper`)

**借了什么**

- CTranslate2 推理后端 + int8 量化，在 CPU 上跑 Whisper 大模型；**确定性**让
  golden 回归成为可能（同一输入同版本必同输出）。
- 本项目强制 `cpu/int8`（ADR-001），并开启 `word_timestamps=True` 拿到词级
  时间戳——这是 V3 一切词级对齐的根。
- `vad_filter`（VAD 参数 `min_silence_duration_ms=500, speech_pad_ms=200`）用于
  切掉非语音段。但正是 `speech_pad_ms=200` 让段级 `start` 带了前导静音，V3 用
  词级边界把这段静音裁掉（见设计文档 §3.1）。

**没借什么 / 为什么**

- 没用它的"逐词对齐"高级封装或 `transcribe` 的额外对齐模式——原生
  `word_timestamps` 已经够用，且零额外依赖。
- 不追求 GPU/浮点更高精度：确定性 > 极限质量（字幕场景够用）。

---

## 2. stable-ts (`jianfch/stable-ts`)

**借了什么（思想，不借依赖）**

- 它的核心思想直接启发了 V3 的 split 设计：**`regroup` 系列操作**——
  `merge_by_gap`（按静音并段）、`split_by_punctuation`（按标点断）、
  `split_by_length`（按长度断）、`split_by_gap`（按静音拆段）。
- V3 的 `_split_by_gap` / `_split_by_length` 在语义上对应 `split_by_gap` /
  `split_by_length`，**顺序也是"先按静音拆、再按长度断"**，和 stable-ts 的
  regroup 哲学一致。

**没借什么 / 为什么**

- **没有引入 stable-ts 依赖**（路线 B 在 Spike 阶段被否决，见 ADR-008）。原因：
  py3.13 无 2.x wheel，1.0.3 损坏且不兼容 faster-whisper/int8。自写两个纯函数
  （几十行）即可覆盖本项目所需，且保持零新增依赖、确定性可复现。

> 这是"借思想、不借代码"的典型：理解别人解决了什么问题，自己用更贴合的
> 最小实现落地。

---

## 3. WhisperX (`m-bain/whisperX`)

**借了什么**

- **词级对齐 + 42 字行宽**的启示：WhisperX 用 forced alignment 把单词对齐到
  精确时间戳，并据此控制字幕行宽。本项目"词级时间戳 + 剪映 42 字单行上限"的
  组合，思想同源。
- "先成句、再断行"的优先级也受其"对齐后按词重组字幕"的思路影响。

**没借什么 / 为什么**

- WhisperX 需要额外的 alignment 模型（wav2vec2 等），依赖更重、且对中文支持
  参差。本项目直接吃 faster-whisper 的 `word_timestamps`，不引入对齐模型。

---

## 4. deep-translator (`nidhaloff/deep-translator`)

**借了什么**

- `GoogleTranslator` 作为无头翻译兜底（`--engine google`）。它的容错与简洁 API
  让"Google 漏翻 → 写 pending"的失败处理很干净。
- 其"翻译一次、失败重试"的模式被本项目 `translate_segments` 的 `MAX_RETRIES`
  借鉴（见 `translate.py`）。

**没借什么 / 为什么**

- 仅作兜底；主路径是 agent 引擎（见下），因为 Google 翻译在"信达雅"和口语感
  上不如真 LLM。

---

## 5. OpenMontage / agent 对话流思想

**借了什么**

- **"agent 即引擎"**（ADR-005）：CLI 只做 CPU 密集的转写，把"需要语言理解的
  翻译"交给调用方 Agent（WorkBuddy / Claude Code 等，它们自带 LLM）。这是把
  "对话式智能体编排"思想落到字幕工具上的结果——CLI 退居"执行者"，Agent 是
  "决策者"。
- 退出的 `EXIT 6 = awaiting agent` 是这个设计的外显信号：工具主动"交还控制权"。

**没借什么 / 为什么**

- 没照搬任何具体 agent 框架，只用了一个极简契约：`<base>.translate_task.json`
  （分批 + 滑动窗口上下文 + persona + glossary）→ Agent 填 `<base>.zh_segments.json`。
  实现轻、可移植、不绑死某家 LLM SDK。

---

## 6. 统一收尾：每个项目"借了 / 没借 / 为什么"

| 项目 | 借了 | 没借 | 为什么 |
|------|------|------|--------|
| faster-whisper | CTranslate2/int8 确定性、`word_timestamps`、VAD | 高级对齐封装、GPU 浮点 | 确定性 > 极限质量；原生已够 |
| stable-ts | regroup 拆分思想（gap/length） | 依赖本身 | Spike 实测不兼容，自写最小实现 |
| WhisperX | 词级对齐 + 42 字行宽思想 | alignment 模型 | 避免重依赖、中文支持参差 |
| deep-translator | Google 无头兜底 + 重试 | 主翻译路径 | 质量不如 LLM agent |
| OpenMontage/agent 流 | agent 即引擎、EXIT 6 交还控制权 | 具体 agent 框架 | 轻契约、不绑死 LLM SDK |

**一以贯之的哲学**：借思想、借确定性、借最小可用；不借重依赖、不借会破坏
"时间戳不重算 / 字节级 golden"根基的东西。
