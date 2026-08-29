# Spec 19 — 人声/伴奏分离预处理 `vocal_sep`

Module: `vocal_sep.py` (new). Integrated into `transcribe.py` + `fill_gaps.py`.
Decision ADR-017.

## Purpose

在转写阶段之前，作为**可选声学清洗层**从原视频音轨中剥离纯人声（vocals.wav），喂给
Whisper / `fill_gaps` 作为输入源，减少强 BGM、哄笑、环境噪音导致的幻觉词与吞字。
**仅换输入源，不改时间轴运算**（ADR-017 §2 声学铁律）。

## Module Interface

### `demucs_available() -> bool`

Lazy probe — 只在第一次调用时尝试 `import demucs.separate`，失败返回 False，成功缓存。
**不得**在模块顶层 import，否则未装 demucs 时基础命令（`generate` / `verify`）
会 ImportError。（demucs 现为默认核心依赖，正常安装即具备；惰性 import 仍保留以兼容
极简环境。）

### `separate_fingerprint(input_path: str, backend: str = "demucs", model_name: str = "htdemucs") -> str`

返回长度 8 的 sha1 hex，覆盖所有会影响分离输出的维度：

```
input_hash = sha1(
    absolute_input_path
    + str(file_size_bytes)
    + str(mtime_seconds)
)[:8]

payload = {
    "input_hash": input_hash,
    "backend": backend,
    "model": model_name,
    "output_sr": 16000,           # Whisper 要求，硬编码
    "output_ch": 1,                # mono，硬编码
    "version": 1,                  # 将来分离算法变了，升级版本让缓存失效
}
fp = sha1(sorted_json(payload))[:8]
```

**关键**：`file_size + mtime` 防止视频文件被替换/覆盖后还复用旧 vocals.wav
（和 chunk cache 的 `transcribe_fingerprint` 模型变更 → 重跑理念一致，
ADR-002 扩展）。

### `vocals_wav_path(outdir: str, base: str, fp: str) -> str`

返回：`{outdir}/{base}.{fp}.vocals.wav`

### `separate_vocals(
    input_path: str,
    outdir: str,
    *,
    base: str | None = None,
    backend: str = "demucs",
    model_name: str = "htdemucs",
    device: str = "auto",           # "auto" → cuda if nvidia-smi else cpu
    progress=print,
) -> str | None`

主入口。返回分离后的 16kHz mono WAV 绝对路径（命中缓存时路径相同，内容不变）。

#### Algorithm

1. `base = base or Path(input_path).stem`
2. `fp = separate_fingerprint(input_path, backend, model_name)`
3. `vocals_path = vocals_wav_path(outdir, base, fp)`
4. **缓存命中判断**：
   - 若 `vocals_path` **文件存在且大小 > 0**
   - **额外断言验证**：`probe_duration(vocals_path) == probe_duration(input_path)` ± 0.05s
     （Demucs 铁律：输出长度 ≡ 输入长度，ADR-017 §2）
   - → 命中：`progress("[skip] vocals cached ({:.0f}s, reuse)".format(dur))`，返回
     `vocals_path`。
5. 未命中 → 执行分离：
   - 若后端不可用（`demucs_available() == False`）→ 返回 `None`（调用方 CLI 负责降级告警）
   - 实际调用 demucs API → 临时输出 2ch 44.1kHz vocals
   - 用 ffmpeg 重采样为 `-ar 16000 -ac 1 -f wav`，落到 `vocals_path`
   - **显存显式释放**：`del model + gc.collect()` + 若 `torch.cuda.is_available()`
     则 `torch.cuda.empty_cache()`（8GB GPU 防 OOM，ADR-017 §5）
6. 返回 `vocals_path`。失败抛异常或 `None`（由调用方决定）。

#### Demucs 调用契约

Demucs 提供多种调用方式；我们选 Python API（而不是 `demucs` CLI），因为：
- Python API 能拿到模型对象引用 → 能 `del` + 释放显存（CLI 子进程退出自动释放也 OK，
  但要自己切 ffmpeg 管道，麻烦）；
- Python API 能精细控制 device / shifts / overlap，与 `transcribe.py` 的 `device=auto`
  策略统一。

推荐调用范式（伪代码，最终以 demucs import 路径为准）：

```python
from demucs import pretrained
from demucs.separate import separate_audio

model = pretrained.get_model(model_name)     # htdemucs
model.to(device_holder)                      # cuda or cpu
# separate_audio 返回 dict[str, Tensor]，键含 "vocals", "drums", ...
out = separate_audio(model, input_tensor, shifts=1, overlap=0.25)
vocals_tensor = out["vocals"]                # [channels, samples] at model sr
# 写入临时 wav → ffmpeg -ar 16000 -ac 1 → 目标 vocals_path
```

**绝不**直接依赖 demucs CLI 的 stdout/stderr 解析——那是脆弱接口。

## Pipeline Integration Points

### (A) `transcribe.py:transcribe_video()` — 主流程接入

```
新增参数: separate_vocals: bool = False
新增参数: demucs_model: str = "htdemucs"

步骤顺序（严格，ADR-017 §5 显存调度）:
  0. outdir mkdirs, base 推导, total duration probe
  1. build transcribe_fingerprint (含 separate_vocals + vocal model)
  2. IF separate_vocals:
       → audio_source = separate_vocals(input, outdir, ...)
       → 若 audio_source is None (未装库 or 失败):
             WARN: "[warn] demucs unavailable; falling back to original audio"
             audio_source = input_path
     ELSE:
       audio_source = input_path
  3. Chunk loop: for each (ci, cstart, cdur):
       → extract_chunk(audio_source, wav, cstart, cdur)  ★ 唯一改动点
       → 其余 Whisper 转写 + 时间戳 + cstart 偏移 → 完全不变
  4. merge + fill_gaps:
       → fill_gaps(..., audio_source=audio_source)  ★ 透传
```

**铁律断言**（写在 pytest 契约测试里，不运行真模型，用合成 fixture）：
`separate_vocals=True` 时 `chunk_N.json` 中 seg.start ∈ [cstart, cstart+cdur]，
即 Whisper 输出时间戳仍相对 chunk 起点正确——因为 `extract_chunk(audio_source)`
的 start/dur 完全没改，只有输入文件指针换了。

### (B) `fill_gaps.py:_decode_once()` — 恢复解码一致化

```
fill_gaps(..., audio_source: str | None = None)
  → _probe(hole_start, hole_end):
       extract_chunk(audio_source or input_path, tmpwav, hole_start-pad, dur)
       → WhisperModel.transcribe(tmpwav)
```

- 若主转写用了 vocals，`audio_source` 就是 vocals.wav，gap 解码也从干净人声切；
- 若主转写未用（separate_vocals=False），`audio_source=None` → 回落 `input_path`，
  行为与现状一致。

### (C) `transcribe.py:transcribe_window()` — resegment 一致化

与 (A) 相同的 `separate_vocals` 参数 + audio_source 切换逻辑，保证
`resegment --separate-vocals` 与主流水线配方一致。

## Invariants (load-bearing)

1. **输出时长 ≡ 输入时长**（Demucs 输出 + 我们的重采样均不得引入时域裁切/补零偏移）。
   缓存命中时用 `probe_duration(vocals) == probe_duration(input)` 验证，断言失败则
   强制重跑分离。
2. **separate_vocals=False（默认）路径字节级不变**——`transcribe_fingerprint`
   hash 与旧代码完全一致，旧 chunk cache 全部复用。
3. **`separate_vocals` 维度必须纳入 chunk fingerprint**——否则
   `separate_vocals=False` 的旧 chunk cache 会被 True 的新运行复用，
   导致配方不一致。
4. **`verify` 声学 lane 永远用原视频**（非 vocals.wav）做 `silencedetect`
   独立参照——否则清洗后的音轨静音区间 ≠ 原事实，出现自证循环（ADR-012 / ADR-017 §6
   Known Limitations）。
5. **优雅降级，永不崩溃**：用户显式 `--separate-vocals` 但未装 demucs →
   WARN + 原路径继续，退出码 0。仅在「装了 demucs 但调用时真正抛异常」时才走
   `EXIT_RUNTIME`（cli.py 外层 try/except 统一接）。

## CLI & Config

### Config 字段（`config.py:Config`）

```
separate_vocals: bool = False    # 对应 env: VT_SEPARATE_VOCALS
demucs_model: str = "htdemucs"   # 隐藏高级参数，env: VT_DEMUCS_MODEL
                                 # 可选: htdemucs, htdemucs_ft, htdemucs_6s, ...
```

### CLI flags

```
# 加到 transcribe / run / resegment 三个子命令:
--separate-vocals          # bool flag，默认 False
--demucs-model NAME        # 隐藏高级，默认 "htdemucs"
```

### `cmd_doctor` 输出

```
  [OPT ] demucs            : not installed — 'pip install -e .' (core dependency) for vocal/BGM separation
  # 或
  [OK  ] demucs (htdemucs) : GPU available (device=cuda)
  # 或 CPU:
  [WARN] demucs (htdemucs) : CPU only — separation will be ~10x realtime, recommend GPU
```

## Test Plan（TDD 先行，先写测试再实现）

### 纯函数单测（`tests/test_vocal_sep.py`，零重依赖，秒跑）

- `test_separate_fingerprint_changes_on_file_mtime`：touch 文件，mtime 变 → fp 变。
- `test_separate_fingerprint_stable_on_same_input`：同文件同参数 → fp 恒等。
- `test_vocals_wav_path_convention`：命名规范断言。
- `test_fingerprint_distinguishes_models`：`htdemucs` vs `htdemucs_ft` → fp 不同。

### Mock / 契约测试（不需真 demucs）

- `test_demucs_unavailable_graceful_fallback`：Mock `demucs_available` → False，
  调 separate_vocals → 返回 None，不抛异常。
- `test_transcribe_fingerprint_includes_separate_vocals`：
  `separate_vocals=True` vs False → transcribe_fingerprint 值不同；
  但 `separate_vocals=False` 时 hash 与旧版（不包含该字段）完全一致。

### Integration（标记 `@pytest.mark.slow`，默认 `pytest` 不跑，`pytest -m slow` 才跑）

- 在 `emily-blunt.mp4`（已知欢呼窗口）上跑 `--separate-vocals`：
  1. 生成的 vocals.wav 时长 == 视频时长 ± 0.05s；
  2. `segments_en.json` 首段 start 与未分离版本之差 < 0.5s（无整体偏移）；
  3. 欢呼窗口（4:06 / 4:24 / 4:59 / 6:15 / 15:29）对应 chunks 的
     `fill_gaps` 强制解码恢复段数 ≥ 关闭分离时的恢复数（召回率 ≥）。

## Failure Modes

| 模式 | 处理 |
|---|---|
| 用户开 flag 但未装 demucs | WARN → 回退原音频，正常继续 |
| demucs 安装 OK，但 import torch 缺 CUDA 运行库 | WARN → 自动用 CPU 跑（doctor 会先提示慢） |
| 分离过程中 demucs 抛异常（OOM / 文件损坏）| CLI 外层 try/except → EXIT_RUNTIME（正常错误处理） |
| 分离成功但 duration 与 input 差 > 0.05s | 丢弃脏 vocals.wav，重跑分离；最多重试 1 次，否则报错 |