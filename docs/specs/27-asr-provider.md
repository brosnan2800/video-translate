# Spec 27 — ASRProvider 接口契约（ASR 层抽离 · 第一步）

- 状态：批准（实现）
- 日期：2026-09-11
- 关联：ADR-038（决策）、ADR-035（数据契约总线）、Spec 02（transcribe 行为）、Spec 22（对齐）

## 范围

- **IN（第一步）**：`ASRProvider` Protocol（含 `prerequisites()`）；`TranscriberConfig` / `TranscribeResult` / `RawSegment` 类型；`FasterWhisperProvider`（薄包装 `transcribe.transcribe_video`）。
- **OUT（第二步）**：`asr.py` 门面 `run_asr()` 与 `cmd_transcribe` 编排搬迁；Provider 自报接线到 doctor / 闸门；层间接缝的物理实现；执行顺序调整（切分与补洞的顺序，见 ADR-038「已知问题」）。
- **OUT（更远）**：引擎阈值画像；第二个 Provider 实现；verify 与 Whisper 自证解耦。

## 不变量（load-bearing）

1. **行为零变化**：第一步**不改变任何现有调用路径与产物**。`cmd_transcribe` 仍直接调 `transcribe_video`；新接口是**旁挂**的，无人调用也不影响任何行为。
2. **契约零变化**：`segments_raw` / `segments` 的字段与形状不动（ADR-035 契约表是唯一事实来源）。
3. **Provider 边界 = 音频 → 原始段**：不含合并 / 补洞等方案后处理。
4. **`RawSegment` 必须满足 `artifacts` 的 `segments_raw.required`**（`start` / `end` / `text`）；置信度字段（`no_speech_prob` / `avg_logprob` / `compression_ratio`）与 `words` 为**引擎可选产出**——缺失时下游签名自动 inert（ADR-020 向后兼容口径）。

## 接口契约

### TranscriberConfig —— 引擎无关的通用转写参数

字段与默认值**必须逐字段等价**于 `transcribe.transcribe_video` 现有形参（TDD 用例锁定）。

| 字段 | 类型 | 默认 | 对应现参 / CLI |
|---|---|---|---|
| `model` | `str` | `"large-v3"` | `model_name` / `--model` |
| `chunk` | `float` | `240.0` | `chunk` / `--chunk` |
| `threads` | `int \| None` | `None` | `threads` / `--threads` |
| `lang` | `str \| None` | `None` | `lang` / `--lang`（None = 自动检测） |
| `vad_threshold` | `float \| None` | `None` | `--vad-threshold` |
| `use_vad` | `bool` | `False` | `--vad`（ADR-034 默认裸跑） |
| `no_speech_threshold` | `float` | `0.0` | `transcribe.NO_SPEECH_THRESHOLD` |
| `temperature` | `list[float] \| None` | `None` | 内部回退温度表 |
| `device` | `str \| None` | `None` | `--device`（None = auto） |
| `compute_type` | `str \| None` | `None` | `--compute-type` |
| `adaptive_vad` | `bool` | `False` | `--adaptive-vad`（ADR-015） |
| `audio_source` | `str \| None` | `None` | T2 人声分离产物（ADR-017） |
| `separate_vocals` | `bool` | `False` | T2 指纹维度 |
| `vocal_sep_backend` | `str` | `"demucs"` | T2 指纹维度 |
| `vocal_sep_model` | `str` | `"htdemucs"` | T2 指纹维度 |
| `vocal_sep_input_hash` | `str \| None` | `None` | T2 指纹维度 |
| `align_backend` | `str` | `"auto"` | `--align`（ADR-028，auto/none/whisperx） |
| `align_allow_degrade` | `bool` | `False` | `--allow-degrade`（控制平面逃生门） |

### TranscribeResult —— 引擎转写结果

| 字段 | 类型 | 说明 |
|---|---|---|
| `segments_path` | `str` | 落盘的 raw segments 路径（`transcribe_video` 的返回值） |
| `segments` | `list[RawSegment]` | 段数据（编排层继续后处理用，避免调用方再读盘） |
| `detected_lang` | `str \| None` | 检测 / 指定的语言（`None` = 引擎自动检测） |

### ASRProvider（Protocol）

```python
class ASRProvider(Protocol):
    name: str
    def prerequisites(self) -> tuple[str, ...]: ...
    def transcribe(self, input_path: str, outdir: str, *, base: str,
                   config: TranscriberConfig, progress=print) -> TranscribeResult: ...
```

- `name`：引擎稳定标识（如 `"faster-whisper"`），用于日志 / state。
- `prerequisites()`：本引擎的就绪要求（能力 id 元组），供 doctor / 闸门做「通用体检 + Provider 自报体检」（ADR-038 D7）。
- `transcribe()`：**唯一必实现方法**，音频 → 原始段。

### FasterWhisperProvider（第一个实现）

- `name = "faster-whisper"`
- `prerequisites()` → `("model:large-v3", "cuda", "whisperx", "demucs")`（与 `capabilities.CAPS` 现有命名对齐）
- `transcribe()`：**薄包装** `transcribe.transcribe_video`，把 `TranscriberConfig` 逐字段展开为关键字参数；随后读回落盘的段数据填充 `TranscribeResult.segments`。**不复制任何逻辑**（行为零变化的前提）。

## TDD 清单

- `TranscriberConfig` 默认值逐字段 == `transcribe_video` 形参默认值（`inspect.signature` 对照，防漂移）；
- `FasterWhisperProvider.transcribe` 把 config 逐字段透传给 `transcribe_video`（mock `transcribe_video` 断言 kwargs）；
- `FasterWhisperProvider.transcribe` 返回的 `segments` 满足 `validate_artifact("segments_raw", ..., require_carry=False)`（且 `segments_path` 一致）；
- `prerequisites()` 返回的能力 id 全部存在于 `capabilities.CAPS`（防拼写漂移）；
- `FasterWhisperProvider` 满足 `ASRProvider` 协议（`isinstance` 结构性检查 / 方法存在性）；
- **全量 `uv run pytest` 零改动全绿**（行为零变化的最终验收）。

## 验收标准

1. 新增 `asr.py` + `tests/test_asr_provider.py`，**现有文件零改动**（除 `docs/index.md` 登记与 `docs` 索引）。
2. 全量 `uv run pytest` 绿，数量只增不减。
3. `cmd_transcribe` / `pipeline` / verify 行为逐字节不变（无调用路径改动）。
