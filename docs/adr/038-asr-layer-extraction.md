# ADR-038 — ASR 层抽离：可插拔 ASRProvider 接口

- **状态**：接受（两步均已落地）
- **日期**：2026-09-11（第二步补注 2026-09-16）
- **关联**：ADR-035（数据契约总线）、ADR-037（产物目录布局）、ADR-030/033（控制平面）、Spec 27（接口契约 · 第一步）、[Spec 28](../specs/28-asr-facade-and-readiness.md)（门面 + 就绪接线 · 第二步）、Spec 02/08/12/13/16/22（被收拢的阶段能力）
- **落地**：`src/video_translate/asr.py`（接口与类型 + `run_asr` 门面 + `engine_prerequisites`）、`transcribe.py`（包装为 Provider）、`model_cache.py`（模型缓存单一来源）、`pipeline.py` / `pipeline_def.py`（引擎前置注入与阶段表收窄）、`cli.py`（`cmd_transcribe` 薄壳 + doctor 按 Provider 自报渲染）、`tests/test_asr_provider.py` / `tests/test_asr_facade.py` / `tests/test_model_cache.py`

## 背景

「语音识别」这一层目前是 6 个模块 + 约 150 行散落在 `cli.cmd_transcribe` 里的编排：

```
vocal_sep.py → transcribe.py(+align.py) → merge.py → fill_gaps.py
```

暴露四个问题：

1. **无可替换接口**：整条链焊死。换 ASR 引擎（Whisper → SenseVoice / 商业 API）要改多处，且无法评估影响面。
2. **编排与实现混杂**：T2 显存调度顺序、静音参照解析、merge/fill_gaps 调用顺序都写死在 CLI 编排函数里，既不好测也不好换。
3. **模型特定逻辑无归属**：`avg_logprob`/`no_speech_prob` 阈值、DTW 塌陷指纹、Whisper 特有的补洞机制散在 `merge.py`/`fill_gaps.py`，换模型时无人知道该改哪些。
4. **就绪检查硬编码引擎**：`capabilities.py` 的 `_probe_model_large_v3()` 写死 `large-v3`、`_probe_whisperx()` 绑死 Whisper 工具链；`pipeline_def.STAGES` 的 transcribe 阶段硬编码 `caps: ["ffmpeg","ffprobe","model:large-v3"]`。换引擎时「环境体检该查什么」也得跟着改。

## 决策

### D1. 分层：ASR 方案层 / 字幕交付层

按「是否与 ASR 引擎强相关」切一刀：

| 层 | 内容 | 引擎相关性 |
|---|---|---|
| **① ASR 方案层** | 人声分离(demucs) → 转写(Whisper) → 对齐(WhisperX) → 幻觉过滤 → 断句合并 → 漏音补洞 → review | **强**（整层随引擎替换） |
| **② 字幕交付层** | 42 字符切分 → 表现层(tail/min-dur) | **无**（剪映交付要求） |

判据：**Whisper 特有的归 ①，剪映交付要求归 ②**。

- 人声分离 / 对齐 / 幻觉过滤 / 补洞 / review 归 ①：分别是「帮 Whisper 听清」「校准 Whisper 的 drift 时间戳」「拦 Whisper 的 DTW 塌陷与自回归幻觉」「用 Whisper 重解码补 Whisper 漏的字」「读 Whisper 置信度字段」——**全部绑死 Whisper**。
- 断句合并归 ①：合并规则（`max_gap` / `max_dur` / 句末标点回退）是针对 **Whisper 的碎片特征**标定的，不同引擎碎法不同、需重调。
- 42 字符切分 / 表现层归 ②：剪映单行上限与阅读呼吸余量，与引擎无关，换任何引擎都一样。

### D2. 接缝：① → ② 交接「句子」

① 层产出**已合并的句子段**（`segments_en.json` 语义），② 层只负责按交付限制切分与表现层调整。这样换引擎时 ② 层一行不改。

**位置约束**：② 必须夹在 P1（转写）与 P2（翻译）之间，**不能放到流水线末尾**。因为 `zh_segments.json` 是 `{index: zh}`，index 对应**最终字幕行**；若先翻译后切分，段数变化会使翻译错行（同 AGENTS.md「40→42 错行事故」陷阱）。

### D3. Provider 接口：① 层内部的可替换插头

① 层内部定义 `ASRProvider` 接口（`asr.py`）：

- **输入**：音频/视频路径 + `TranscriberConfig`（引擎无关的通用转写参数）+ 进度回调
- **输出**：`TranscribeResult`（落盘路径 + raw 段数据 + `detected_lang`）
- 第一个实现：`FasterWhisperProvider`（包装现有 `transcribe.transcribe_video`）

**接口边界 = 「音频 → 原始段」**（不含后处理）。理由：后处理（幻觉过滤 / 合并 / 补洞）虽也是 Whisper 特有、同归 ① 层，但**不是「引擎」的职责**，而是「围绕引擎的方案逻辑」，通过接口回调引擎（如 `fill_gaps` 需重新解码）。把后处理塞进引擎接口会让每个新 Provider 重写一遍整理逻辑。

### D4. 契约零变化

`segments_raw.json` / `segments_en.json` 的字段与形状**零变化**（ADR-035 契约表仍是唯一事实来源）。本 ADR 只重组代码结构，不改产物、不改控制平面、不改 verify。

### D5. 分两步实施

- **第一步（本 ADR，行为零变化）**：定义 `ASRProvider` Protocol（含 D7 的 `prerequisites()`）+ `TranscriberConfig` / `TranscribeResult` + `FasterWhisperProvider` 包装 `transcribe_video`。**不改任何现有调用路径**，可独立回归。
- **第二步（后续单独 ADR/Spec）**：新建 `asr.py` 门面 `run_asr()`，把 `cmd_transcribe` 的编排搬入；把 D7 的 Provider 自报接线到 doctor / 闸门；此时才处理层间接缝与执行顺序调整（见「已知问题」）。

> **补注（2026-09-16 / [Spec 28](../specs/28-asr-facade-and-readiness.md)）— 第二步已落地**：
> - **A 块**：`asr.run_asr()` 门面 + `cmd_transcribe` 薄壳（编排收进 ① 层；产物逐字节不变，经实弹冒烟）。
> - **B 块（D7 接线）**：能力分**通用**（`ffmpeg`/`ffprobe`）与**引擎特定**；
>   `pipeline_def.STAGES` 的 transcribe 阶段 `caps` 收窄为通用前置，引擎特定前置由
>   `asr.engine_prerequisites()` 自报、`pipeline.build_ctx` 注入 `_engine_caps` 并在
>   `check_stage` 合并检查；doctor 的模型体检项改为按 Provider 自报渲染；`model:<name>`
>   型能力 id 由**前缀解析**（新引擎声明 `model:sensevoice` 无需改 `capabilities.py`）；
>   模型缓存探测下沉为单一来源 `model_cache.py`，顺带解掉 `capabilities` → `cli`
>   的循环依赖与三处 `<repo>/models` 重复推导。
> - **仍未做**：执行顺序调整（切分与补洞↔短句合并/孤儿并右的顺序），属**行为变更**，
>   见下「已知问题」。

### D6. P0 拆解：通用基础设施 / 引擎特定就绪 / 翻译前置

P0（preflight）现状是一个阶段混了三类不同归属的东西，按归属拆开：

| 块 | 内容 | 归属 | 换引擎后 |
|---|---|---|---|
| **通用基础设施** | `ffmpeg` / `ffprobe` 探测；**音频画像**（`duration` / `silence_intervals` / `mean_db` / `max_db` / `silence_fraction`） | 独立「通用环境」 | **不变** |
| **引擎特定就绪** | `model:large-v3` / `whisperx` / `demucs` / `cuda` 探测；VAD 与人声分离 routing | ① ASR 层（由 Provider 声明） | 随引擎变 |
| **翻译层前置** | 决策点（选翻译风格，默认 film） | 翻译层 | 不变 |

**通用部分不随引擎变**的依据：音频画像是音频的**客观属性**（由 `ffmpeg silencedetect` / `volumedetect` 独立测出），与用哪个 ASR 无关；`ffmpeg`/`ffprobe` 是所有引擎的共同前置（抽音频、测静音、探时长都要它）。verify 的声学 lane 也正是靠这份「通用音频画像」做**独立验证**——所以它必须与引擎解耦。

### D7. Provider 自报就绪要求

`ASRProvider` 增加 `prerequisites()`，把「引擎特定的就绪检查」从硬编码的表里交还给引擎自己：

```python
class ASRProvider(Protocol):
    name: str
    def prerequisites(self) -> tuple[str, ...]: ...   # 能力 id，如 ("model:large-v3","cuda","whisperx","demucs")
    def transcribe(self, ...) -> TranscribeResult: ...
```

- `FasterWhisperProvider.prerequisites()` → `("model:large-v3", "cuda", "whisperx", "demucs")`
- doctor / 闸门 = **通用体检（静态固定：ffmpeg/ffprobe）+ Provider 自报体检（动态）**
- 换引擎时新 Provider 自己声明要查什么，**不改 `capabilities.py`**
- `pipeline_def.STAGES` 的 transcribe 阶段 `caps` 收窄为**通用前置**（`ffmpeg`/`ffprobe`）；引擎特定前置由 `pipeline.py` 组装 ctx 时从 Provider 注入

**代价**：能力集合从「全局静态」变为「取决于当前引擎」。STAGES 保持纯数据的办法 = 只声明通用前置，引擎特定的在引擎层组装时注入。

## 理由

- **可替换性**：换引擎 = 实现一个 Provider（含它自己的就绪声明 + 字段映射），影响面从「6 个模块 + 若干硬编码探测」收敛到「1 个接口」。
- **模型特定逻辑有归属**：所有 Whisper 特有阈值、几何指纹与就绪检查集中在 ① 层，换模型时改动边界清晰——直接回应「换掉 Whisper 会不会很多逻辑作废」：**作废范围可枚举**。
- **② 层与引擎解耦**：交付要求（剪映限制 / 阅读节奏）不因换引擎而漂移。
- **通用层独立**：ffmpeg / 音频画像既服务 ①，也服务 verify 的独立验证——它必须是与引擎无关的中立设施。
- **对 pipeline 零影响**：对外仍是 `segments_raw.json` + `segments_en.json`，控制平面 / 数据契约 / verify（本轮）全不动。

## 后果

- **正面**：ASR 层有了明确边界与可替换插头；新增引擎只需实现 `ASRProvider`；P0 的三类归属清晰，就绪检查不再硬编码引擎。
- **代价 / 注意**：
  - 第一步引入一层薄间接（`FasterWhisperProvider` 包装 `transcribe_video`），**暂不减少代码量**，只建立接口。
  - D7 使 capability 集合动态化：`capabilities` 与 `pipeline_def` 需配合调整（第二步接线）。
  - **已知问题（第二步处理）**：当前执行顺序为「过滤 → 合并 → 漂移吸附 → **切分** → 短句合并 → 孤儿并右 → **补洞**」——切分夹在合并与补洞之间，其后还有短句合并 / 孤儿并右 / 补洞三步作用于「切分后的段」。严格按 D2（切分归 ②）需调整执行顺序，属**行为变更**，必须在第二步单独评估与回归，不得顺手改。

## 未来（不在本 ADR 范围）

- **引擎阈值画像**：`avg_logprob=-0.95` / `no_speech_prob=0.6` / `_NSP_SHORT_WORDS=6` / `_ECHO_OVERLAP=1.0` 等随 Provider 声明，换模型时集中重标定（解决「阈值散落 4 个模块 10+ 处」）。
- **第二个 Provider 实现**（SenseVoice / 商业 API）：验证接口可替换性，是接口设计的最终验收。
- **verify 与 Whisper 自证解耦**：`find_low_confidence_segments` / `review` 信号 A 的处置——裁判只看选手交出的成品，不看选手的内心活动；低置信道归位到 ① 层内部自检。**已落地（[ADR-041](041-verify-decoupled-from-asr-self-report.md)，2026-09-16）**：低置信道整体移除，`review` 信号 A 保留为 ① 层内部自检。
