# Spec 24 — 大空档人声分离召回（`gap_vocal_sep`）

模块：`src/video_translate/gap_vocal_sep.py`。在 `fill_gaps.py` 内部被调用，作为 ADR-016/021 之上的可选增强层。

## 目的

对“长度大 + 音频能量高 + 仍被漏掉”的时间洞（hard gaps），临时做 Demucs 人声分离，再从干净人声解码，以召回被笑声、欢呼、强 BGM 压住的真实语音。同时用比常规恢复段更严的过滤器拦截 BGM 歌词/笑声幻觉。

## 模块接口

```python
def recover_hard_gaps(
    input_path: str,
    segments: list[dict[str, Any]],
    holes: list[tuple[float, float]],
    *,
    min_gap: float = 5.0,
    energy_mean_db: float = -30.0,
    energy_max_db: float = -10.0,
    audio_source: str | None = None,
    progress: Callable[..., None] = print,
) -> dict[tuple[float, float], str]:
    """Return map of hard gap -> vocals_wav path.

    The caller (`fill_gaps`) uses this mapping to decode hard gaps from the
    cleaned vocals source instead of the original video. Keeping decode inside
    `fill_gaps` avoids loading WhisperModel twice and respects ADR-017's
    Demucs/Whisper sequential GPU scheduling.
    """
```

```python
def select_hard_gaps(
    input_path: str,
    holes: list[tuple[float, float]],
    *,
    min_gap: float = 5.0,
    energy_mean_db: float = -30.0,
    energy_max_db: float = -10.0,
    progress: Callable[..., None] = print,
) -> list[tuple[float, float]]:
    """Filter holes down to high-energy hard gaps worth running Demucs on."""
```

```python
def _is_gap_vocal_hallucination(
    cand: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    no_speech_thr: float = 0.5,
    avg_logprob_thr: float = -0.8,
    overlap_eps: float = 0.12,
    max_wps: float = 8.0,
    min_zero_dur_words: int = 2,
) -> bool:
    """Strict hallucination guard for gap-vocal-sep recovered segments."""
```

```python
def _looks_like_lyrics(text: str) -> bool:
    """Heuristic: detect repetitive/chant-like text typical of BGM lyrics."""
```

```python
def _decode_gap_vocals(
    vocals_map: dict[tuple[float, float], str],
    *,
    model: Any,
    lang: str | None,
    segments: list[dict[str, Any]],
    no_speech_thr: float = 0.5,
    avg_logprob_thr: float = -0.8,
    progress: Callable[..., None] = print,
) -> list[dict[str, Any]]:
    """Decode each hard gap from its cleaned vocals.wav and filter strictly."""
```

## 算法

1. **硬洞选择**（`select_hard_gaps`）
   - 输入 `fill_gaps` 已经过 silence filter 的 holes。
   - 对每个洞 `(gs, ge)`：
     - 若 `ge - gs < min_gap` → 跳过；
     - 用 ffmpeg `volumedetect` 分析窗口 `[gs, ge]` 的 mean/max dB；
     - 若 `mean_db <= energy_mean_db` 且 `max_db <= energy_max_db` → 跳过；
   - 返回剩余洞列表。

2. **人声分离**（`recover_hard_gaps`）
   - 若 `audio_source` 不为空且文件存在 → 把每个 hard gap 映射到 `audio_source`，跳过 Demucs；
   - 否则对每个 hard gap：
     - `extract_chunk(input_path, tmp_wav, gs, ge - gs)`
     - 调用 `separate_vocals` 对临时文件分离（复用 `vocal_sep.py`，输出到临时目录）
     - 断言 `probe_duration(vocals_wav) == (ge - gs) ± 0.05s`，否则丢弃该洞
     - 记录 `gap -> vocals_wav` 映射
   - 完成后显式释放 Demucs GPU 内存。

3. **解码与过滤**
   - 把 `gap -> vocals_wav` 映射交给 `fill_gaps` 的解码逻辑；
   - 对从 gap vocals 解码出的候选段，依次经过：
     - `_is_echo`（文本相似去重）
     - `_is_gap_vocal_hallucination`（严格守卫）
   - 通过者插入时间轴。

## 严格守卫（`_is_gap_vocal_hallucination`）

在 ADR-021 的 `_is_recovered_hallucination` 基础上收紧：

| 信号 | 阈值 | 说明 |
|---|---|---|
| A. 与现有段重叠 | 词数 < 4 且重叠 > 0.12s | 同 ADR-021 信号 A |
| B. 语速异常 | 词数 ≥ 2 且 wps > 8.0 | 同 ADR-021 信号 B |
| C. 非语音自判 | `no_speech_prob >= 0.5` | 比常规 0.6 更严 |
| D. 零时长词 | ≥ 2 个 | 同 ADR-021 信号 D |
| E. 低置信度 | `avg_logprob < -0.8` | 比常规 -1.0 更严 |
| F. 歌词/重复模式 | 同一 bigram 重复 ≥ 2 次，或首尾 token 相同且 nsp ≥ 0.4 | 拦截 BGM 歌词 |

任一命中即丢弃。

## 默认值

| 参数 | 默认值 | 说明 |
|---|---|---|
| `gap_vocal_sep` | `False` | 总开关 |
| `gap_vocal_sep_min_gap` | `5.0` | 硬洞最小长度（秒） |
| `gap_vocal_sep_energy_mean_db` | `-30.0` | mean dB 阈值 |
| `gap_vocal_sep_energy_max_db` | `-10.0` | max dB 阈值 |
| `gap_vocal_sep_no_speech_thr` | `0.5` | 严格 nsp 阈值 |
| `gap_vocal_sep_avg_logprob_thr` | `-0.8` | 严格 avg_logprob 阈值 |

## CLI & Config

### Config 字段

```python
gap_vocal_sep: bool = False
gap_vocal_sep_min_gap: float = 5.0
gap_vocal_sep_energy_mean_db: float = -30.0
gap_vocal_sep_energy_max_db: float = -10.0
gap_vocal_sep_no_speech_thr: float = 0.5
gap_vocal_sep_avg_logprob_thr: float = -0.8
```

对应环境变量前缀 `VT_GAP_VOCAL_SEP_*`，例如 `VT_GAP_VOCAL_SEP=1`、`VT_GAP_VOCAL_SEP_MIN_GAP=4.0`。

### CLI flags

加到 `transcribe`、`run` 子命令：

```
--gap-vocal-sep                       # bool flag
--gap-vocal-sep-min-gap SECONDS       # default 5.0
--gap-vocal-sep-energy-mean-db DB     # default -30.0
--gap-vocal-sep-energy-max-db DB      # default -10.0
--gap-vocal-sep-no-speech-thr THR     # default 0.5
--gap-vocal-sep-avg-logprob-thr THR   # default -0.8
```

## 测试计划（TDD）

### 纯函数单测（`tests/test_gap_vocal_sep.py`）

- `test_select_hard_gaps_filters_by_duration`：短洞被过滤。
- `test_select_hard_gaps_filters_low_energy`：静音洞被过滤。
- `test_select_hard_gaps_keeps_high_energy_long_gap`：长且响的洞保留。
- `test_looks_like_lyrics_detects_repetition`：重复 bigram 被识别为歌词。
- `test_is_gap_vocal_hallucination_stricter_than_regular`：nsp=0.55 被新守卫丢弃、被旧守卫保留。
- `test_is_gap_vocal_hallucination_keeps_real_recovery`：真实恢复段不误杀。

### Mock / 集成测试

- `test_recover_hard_gaps_reuses_global_vocals`：`audio_source` 存在时不再调 Demucs。
- `test_recover_hard_gaps_runs_demucs_per_gap`：Demucs 对每个硬洞调用一次。
- `test_recover_hard_gaps_skips_mismatched_duration`：时长不一致的 vocals.wav 被丢弃。

### 回归

- 跑 `tests/test_fill_gaps_bare.py` 与 `tests/test_fill_gaps_recovered_guard.py`，确保 `gap_vocal_sep=False` 时行为不变。

## 不变量

- `gap_vocal_sep=False` 时 `fill_gaps` 输出与修改前完全一致。
- 临时 vocals.wav 时长必须等于原洞窗口时长，差值 ≥ 0.05s 即丢弃。
- Demucs 与 Whisper 在 GPU 上串行执行。
- 恢复段时间戳仅按窗口起点平移，不重算。
