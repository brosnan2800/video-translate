# 引用的外部项目思想（附录）

> 本章专讲：本项目从哪些优秀开源项目里"借了什么、没借什么、为什么"。
> 正文的原理与机制见 `translation-design.md`。这里只做思想溯源，便于维护者
> 理解"为什么长这样"，以及将来要升级该往哪看。

---

## 0. 按运行模式一图看懂（先看这张表）

本项目跨 CPU（Mac）/ GPU（Windows）双运行模式，外部借鉴按**模式**分成三类，
避免"借思想"和"真引入依赖"混为一谈。逐项目详细说明见 §1–§5，Voice-Pro 研究
见 `RESEARCH-voice-pro.md`。

| 运行模式 | 项目 | 借了什么 | 怎么用 | 依赖/限制 |
|---|---|---|---|---|
| **1. CPU 模式借鉴** | faster-whisper | CTranslate2/int8 确定性、`word_timestamps`、VAD | 直接依赖（CPU 主引擎） | 锁 `1.2.1`（ADR-001），CPU/int8 |
| | Demucs | 人声分离 | 直接依赖（`--separate-vocals`，T2 已过） | CPU/GPU 均可 |
| | deep-translator | Google 无头翻译兜底 + 重试 | 直接依赖（`--engine google`） | 仅兜底，主路径是 agent |
| | stable-ts | regroup 拆分思想（gap/length） | **只借思想**，自写 `merge.py` 纯函数 | **拒收依赖**：py3.13 无 wheel（ADR-008） |
| **2. GPU 模式直接引用** | CUDA / torch | venv 内 cu124 wheel 自带 CUDA 运行时 | 直接引用（E4：`venv torch/lib` 自动探测） | GPU 盒；显式 `VT_CUDA_DIR` 覆盖 |
| | WhisperX | wav2vec2 强制对齐（词级时间戳 82%→96%） | **计划直接引用**（`--align whisperx`，仅 `[windows]` extra） | **T4 待落地**（ADR-013 延后，未实现） |
| | pyannote | 说话人分离（Diarization） | 计划直接引用（`--diarize`） | T5 待落地，需 `HF_TOKEN` |
| **3. 其他思想借鉴** | OpenMontage / agent 流 | "agent 即引擎"、EXIT 6 交还控制权 | 自实现极简契约（`translate_task.json`），**不引框架** | 不绑死某家 LLM SDK |
| | WhisperX（思想） | 词级对齐 + 42 字行宽 + "需独立参照校验" | ADR-012 用 ffmpeg `silencedetect` 落地（`audio_profile`） | 零新依赖，Mac 可行 |
| | Voice-Pro | uv.lock 可复现、便携 ffmpeg、wheel 自带 CUDA、模型自愈 | E1–E4 已全部落地 | 见 `docs/TOOLING.md` |

**三条记忆要点：**
1. **CPU 模式**：能"真引入"的才引入（faster-whisper / Demucs / deep-translator）；
   引入不了的（stable-ts 无 wheel）→ **只借思想**，自写最小实现。
2. **GPU 模式**：才值得真引入重依赖（WhisperX / pyannote），但这两项是 **T4/T5 待落地**，
   当前 GPU 已直接用的是 CUDA 加速（E4）+ Demucs（T2）。
3. **思想借鉴**：OpenMontage（agent 即引擎）、WhisperX（需外参照校验）、Voice-Pro
   （工程最佳实践）——不引依赖，只吸收设计哲学。

> 铁律（MAJOR_VERSION_PLAN §0.2）：所有 GPU/Windows 专享特性均为**增量可选**，
> Mac/CPU 下必须自动平滑降级，绝不破坏基础流水线。

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

- **词级对齐 + 42 字行宽的启示**：WhisperX 用 forced alignment 把单词对齐到
  精确时间戳，并据此控制字幕行宽。本项目"词级时间戳 + 剪映 42 字单行上限"的
  组合，思想同源。
- "先成句、再断行"的优先级也受其"对齐后按词重组字幕"的思路影响。
- **最重要的启发——"faster-whisper 词级时间戳不够可信"**：WhisperX 之所以
  存在，正是因为裸 faster-whisper 的 `word_timestamps` 是 **DTW 后验估计**，
  会漂移/塌陷。这直接引出本项目的声学层核心判断（ADR-012）：

  > 词级时间戳 ≠ 声学事实。必须对照独立声学参照校验，而不是 whisper 自证。

  由此落地了「**检测 + 路由**」（Mac 可行、零新依赖）：用 ffmpeg `silencedetect`
  / `volumedetect` 做独立参照源（`audio_profile`），`doctor` 按音频画像自动路由
  VAD，`verify` 三 lane 里的**声学 lane** 拿静音区间核对每个 cue——这是把
  WhisperX「对齐要外部分参照」的哲学，用不引入重依赖的方式移植了进来。

**没借什么 / 为什么（关键：是"延后"不是"永不"）**

- **Mac 当前不引入 wav2vec2 alignment 模型**：依赖更重、要 py3.12+CUDA、且对
  中文支持参差；Mac 锁 `faster-whisper==1.2.1` + CPU/int8 确定性（ADR-001），
  零新依赖哲学要求保住 golden 回归。
- **但真正的 WhisperX 强制对齐（~96% 精度）并未被永久拒绝——是 GPU 盒专用、
  延后落地的路径**（ADR-013，对应 MAJOR_VERSION_PLAN **T4**；ADR-013 内部记作 T3，
  本文统一用 T4）：`--align {auto,none,whisperx}` 默认 `auto`（T4 默认化：GPU 即 whisperx），
  仅 `[windows]` extra，对齐只润词级/显示时间戳、不改段落语义；显式请求但库
  不可用（典型 Mac）时告警并自动回退 `none`。参考关系从"借鉴思想"升级为
  "**未来在 GPU 路径真正采用其算法**"。

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
| Demucs | 人声分离（`--separate-vocals`） | —（直接依赖，T2 已过） | CPU/GPU 均可；见 Spec 19 / ADR-017 |
| stable-ts | regroup 拆分思想（gap/length） | 依赖本身 | Spike 实测不兼容，自写最小实现 |
| WhisperX | 词级对齐 + 42 字行宽 +「时间戳需外参照校验」哲学（ADR-012 声学 lane） | Mac 的 wav2vec2 alignment 模型；算法本体留 GPU 盒 **T4** 落地（ADR-013 延后，未实现） | 避免重依赖/中文参差；GPU 强制对齐（96%）待落地，Mac 只检测+路由 |
| deep-translator | Google 无头兜底 + 重试 | 主翻译路径 | 质量不如 LLM agent |
| OpenMontage/agent 流 | agent 即引擎、EXIT 6 交还控制权 | 具体 agent 框架 | 轻契约、不绑死 LLM SDK |
| Voice-Pro | uv.lock、便携 ffmpeg、wheel 自带 CUDA、模型自愈 | WebUI、一站式堆功能 | E1–E4 已落地；见 RESEARCH-voice-pro.md |

**一以贯之的哲学**：借思想、借确定性、借最小可用；不借重依赖、不借会破坏
"时间戳不重算 / 字节级 golden"根基的东西。
