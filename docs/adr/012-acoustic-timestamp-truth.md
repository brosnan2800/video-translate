# ADR-012 — 修订「时间戳是声学事实」不变量（引入独立声学参照）

- 状态：接受
- 日期：2026-08-18
- 关联：Spec 00（不变量，待修订）、ADR-011（VAD 选开）、Spec 16（fill-gaps）、Spec 17（verify_align）、Spec 18（verify 三 lane）、ADR-008（stable-ts 拒收）、MAJOR_VERSION_PLAN T3（WhisperX/GPU）

## 背景
Spec 00 的设计不变量写道：

> **Timestamps are acoustic facts, never recomputed.** faster-whisper 的 `start`/`end`
> 原样贯穿整条流水线；翻译只改文本。这保证音频/字幕对齐「for free」。

这条不变量在 **V1 段级边界** 上大体成立，但 **V3 引入词级时间戳后从未被 ADR 修订**。
词级时间戳是 **DTW 后验估计**，会塌陷 / 漂移，≠ 声学事实（项目 `MEMORY.md` V4 早已承认）。
把它静默延伸到根本扛不住它的数据上，是 V4–V13 一连串补丁、以及本次 IF 实战踩坑的共同根因。

本次 IF（Michael Caine 朗诵 Kipling《If—》，干净单人录音，140.7s）实证：
- 用 `--no-vad` 裸跑：`silencedetect` 实测片头静音 0→6.96s、多处 1–1.5s 停顿、片尾
  131.20→140.67s；但 whisper 把真实静音**跨接**成连续语音，段边界 token **累积飘
  移（非单调）**——开头对、越往后越偏，单点全局偏移无法修正。
- 开 `--vad` 重转：段边界被钉在真实静音上，漂移从根上消除。
- 另有表现层问题：V2 显式传 `--tail 0 --min-dur 0` 削掉默认显示留白，导致字幕
  「早出现 / 早消失」——属独立于声学层的第三维度。

诊断结论（见 Spec 18）：字幕正确性 = **声学层 / 内容层 / 表现层** 三个正交维度，
此前被揉成一团。最致命的声学层建立在「模型输出 = 真相」的假前提上。

候选方案：
- **(A) 维持现状**：继续依赖 whisper 自带 DTW 时间戳自证，靠 agent 临场比对
  `silencedetect` 救火。代价：每次都纠结 VAD、漂移靠人耳发现。
- **(B) 引入独立声学参照 + 自动路由 + 分层校验（本 ADR）**：承认词级时间戳≠声学
  事实，用 `ffmpeg silencedetect` 作为独立参照校验对齐（Mac 零新依赖）；`doctor`
  按音频画像自动路由 VAD；新增 `verify` 三 lane 统一自检；修订 Spec 00 不变量；
  真正强制对齐（WhisperX）仍留 GPU（ADR-008 已确认 Mac 上 stable-ts 不可装）。

## 决策
选 **(B)**，分两条主线：

1. **修订 Spec 00 不变量**（见下「修订后不变量」）：时间戳仍原样传递、不重算，
   但**不再把它当作声学事实自证**；对齐必须对照独立参照（`silencedetect` 实测
   静音）校验；段边界应优先以 VAD / 静音为锚。
2. **补一个真实的声学 ground truth 通道**（Mac 可行、零重依赖）：
   `audio_profile`（volumedetect 电平 + silencedetect 静音区间）作为唯一独立参照
   源，供 `doctor` 路由与 `verify` 声学 lane 复用。

### 修订后的 Spec 00 不变量（替代原第 8–12 行）
> **时间戳原样传递、不重算**（保持）。但 **faster-whisper 的词 / 段时间戳是 DTW
> 后验估计，会漂移 / 塌陷，不是声学事实**。因此：
> - 段边界优先以 **VAD / 静音** 为锚（见 ADR-011 的内容类型路由）；
> - 对齐必须对照 **独立参照**（`silencedetect` 实测静音区间）校验，而非 whisper
>   自证；
> - 真正「修复」声学层（强制对齐，96% vs 词级 82%）依赖 WhisperX，锁死 GPU
>   （见 MAJOR_VERSION_PLAN T3）；Mac 路径只做「检测 + 路由」，不做「修复」。

## 理由
- **假前提已证伪**：IF 实战直接证明裸跑 + 词级时间戳在干净录音上也会累积漂移。
  继续把它当 invariant 只会让补丁越打越厚。
- **Mac 边界必须接受**：ADR-008 已记录 stable-ts 在 py3.13 装不上、WhisperX 要
  py3.12+CUDA。把「修时间轴」当 Mac 目标是方向性误判；Mac 该做的是「检测 + 路由」。
- **显示层 ≠ 声学层**：`offset/tail/min-dur` 是感知层修正（`generate.py` 已写明
  绝不改动底层声学时间戳），必须三层分离，否则「早出现 / 早退」与「漂移」会被混为一谈。
- **最小依赖**：`silencedetect` / `volumedetect` 都是 ffmpeg 自带滤镜，零新重依赖，
  与现有 `fill_gaps` / `doctor` 基础设施天然契合。

## 后果
- **Spec 00** 第 8–12 行不变量按上方「修订后不变量」改写（本 ADR 生效后即办）。
- **新增 `docs/specs/18-verify.md`**：定义 `verify` 命令的三 lane（声学 / 内容 /
  表现）契约，复用 `silencedetect` 参照与现有 `validate_zh` / `verify_align`。
- **新增 `src/video_translate/audio_profile.py`**：`AudioProfile` dataclass
  （mean_vol / max_vol / silence_intervals），封装 `ffmpeg` 两滤镜。
- **`cmd_doctor`** 接 `AudioProfile` → 自动输出 VAD 路由建议（非致命）。
- **`cmd_verify`**（Spec 18）：声学 lane 用 `silencedetect` 区间 vs 每个 cue 报
  「落在静音 / 跨静音 / 首句提前」；表现 lane 报 `tail/min-dur` 被削 sanity。
- **`fill_gaps`**（Spec 16）：对 HEAD / TAIL **纯静音窗** 不强解（先 `silencedetect`
  判真静音），堵「恢复出 Hubsan 孤立幻觉」的洞。
- **`drop_hallucination_segments`**（`merge.py`）：补「段整体落在静音窗」孤立幻觉
  信号（原只防「塌陷 + 邻居 3-gram」双信号，漏过头部静音孤立幻觉）。
- **AGENTS.md**：从「V2 + V3–V13 changelog」重构为「当前真相 + 独立 HISTORY」；
  新增「显示窗不得过度收紧」「§5 自检改为跑 `verify`」铁律；`.workbuddy/memory`
  MEMORY.md 中 load-bearing 经验上移进仓库文档（跨工具可读）。
- **语义保真**（内容层第三道）作为 agent 侧步骤（`verify --semantic` 吐回读任务，
  CLI 不引 LLM 客户端，守 ADR-005），见 Spec 18 Phase 2。

## 已知限制
- Mac 上声学层只能「检测 + 路由」，无法真修复词级时间戳漂移；彻底修复依赖 GPU
  盒的 WhisperX（T3）。
- `silencedetect` 参照精度受 `noise` / `d` 阈值影响；默认 `noise=-30dB,d=0.3`，
  与 `audio_profile` 共用，必要可调。
