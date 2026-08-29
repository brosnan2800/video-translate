# ADR-017 — 人声/伴奏分离预处理（Demucs，可选声学清洗层）

Date: 2026-08-21
Status: Accepted
Companion: ADR-012 (声学铁律), ADR-016 (fill_gaps 召回网), Spec 19 (vocal_sep 行为契约),
MAJOR_VERSION_PLAN T2.

## Context

在战争片（如《天国王朝》）、预告片、重 BGM、哄笑/环境杂音或音乐覆盖的视频中，伴奏与底噪会
进入 Whisper 的编码器注意力，产生两类**仅靠 VAD/后处理无法根治**的声学错误：

1. **低置信度幻觉词**："Saladin" 这类配乐下的台词被背景旋律干扰，Whisper 内部语言模型
   用高频 n-gram 填满，产生 "how are you" / "the movie is a movie" 类重复幻觉；
2. **微弱吞字**：笑声、欢呼或音乐垫底下的真实单词被 VAD 或 Whisper 自有的 no_speech_gate
   当作「非语音」静默剔除，`fill_gaps`（ADR-016）的裸跑恢复可以救一部分，但当背景噪声
   能量明显高于人声时，强制解码仍会产出空段或错段。

在 `emily-blunt.mp4` 的 5 个欢呼-掩码窗口中，若先把人声从背景里剥离，`transcribe` 与
`fill_gaps` 的召回率均会上升；同时纯人声音轨能直接提升后续 `--adaptive-vad`（ADR-015）
的路由判断质量（Silero VAD 不会再把笑声当作 speech anchor）。

### 候选方案评估

| 方案 | 依赖 | 质量 | 速度 | 跨平台 | 是否引入延迟/相位偏移 |
|---|---|---|---|---|---|
| (A) **Demucs (htdemucs)** 【本 ADR】 | demucs + torch (extra 隔离) | 优（业界 SOTA 轻量版） | 中（GPU 约 0.5x 实时） | Win/Mac/Linux | **无**（Demucs 源码保证输出长度与输入严格一致） |
| (B) MDX-Net / UVR5 | onnxruntime + 自定义模型 | 优 | 快 | 跨平台 | 模型依赖不透明，社区权重分发不可控 |
| (C) spleeter (tf) | TensorFlow | 中 | 快 | 跨平台 | TF 与 PyTorch 同驻 8GB GPU 必 OOM；项目已停更 |
| (D) ffmpeg afftdn / high-pass | 零额外依赖 | 差（仅滤稳态底噪，对人声+伴奏混叠几乎无效） | 极快 | 跨平台 | — |

**方案 (D) 先被排除**：高通+去噪只能去风扇/空调类稳态噪声，对音乐伴奏混叠无效；
**方案 (C) 被排除**：TF+PyTorch 8GB 同驻必炸；
**方案 (B) 被排除**：权重分发流程不可控（需要用户手动下载 2-3 个 100MB+ pth，
不在 HF Hub），而 Demucs 的 `htdemucs` 权重走 HF Hub 官方渠道，
配合 `VT_MODEL`/`HF_HOME` 现有缓存基建可直接复用。
因此选 **(A) Demucs**，但严格做「可选 extra + 优雅降级」。

## Decision

### 1. 定位：转写前的**可选声学清洗层**（默认关，零破坏）

`separate_vocals=False`（默认）路径与现状**字节级一致**——chunk cache fingerprint
不包含 vocal_sep 字段时与旧 hash 完全相同，所有历史 chunk_N.json 100% 复用，
golden 回归零变更。

### 2. 声学对齐铁律守护（ADR-012 约束）

- 分离出的**纯人声音轨（vocals.wav）仅用于喂给 Whisper 做文本识别与词切分**；
- 起止时间戳**100% 对应原视频真实时间轴**——`plan_chunks(start/dur)`、
  `extract_chunk(start/dur)`、Whisper 输出的 `start/end`、后续 `merge`/
  `fill_gaps`/`generate` 的时间运算**全部不变**；
- **不得**对 vocals.wav 做任何破坏性重采样（除了 Whisper 要求的 16kHz mono）；
  不得对分离输出做时域裁切 / 补零 / 相位校正引入偏移。Demucs 输出长度 = 输入长度，
  这点是硬约束，上线前要写断言测试。

### 3. 依赖隔离（跨平台 + 优雅降级）

- `demucs` / `torch` / `torchaudio` 已移入 `pyproject.toml` 的**核心 `dependencies`**，
  因此默认 `pip install -e .` 与 `pip install -r requirements.txt` 都会把它装进当前
  环境（不再需要额外的 `.[audio]` 步骤）。CUDA 版与 CPU 版 torch wheel 由
  `[tool.uv.sources]` 按平台自动选择：Windows/Linux 拉 CUDA 版，macOS 拉 CPU 版。
- CLI flag `--separate-vocals`（语义优先，不锁死 demucs 后端名）：
  - 若未安装 `demucs` → **WARN 打印并自动回退原路径（退出码 0，不崩溃）**；
  - 若安装成功 → 正常启用。
  这条与 WhisperX 的降级模式（ADR-013）统一，即「GPU / 重型特性是增量可选，
  绝不破坏基础流水线」。

> **变更记录（2026-08-21）**：原设计把 demucs 放在 `[audio]` optional-dependencies，
> 导致标准安装（`pip install -e .`）漏装它、依赖飘到系统 Python。现已改为核心依赖，
> 对齐 pyvideotrans 的「主 deps + uv 按平台选 wheel」做法。

### 4. 缓存指纹 + 中间产物持久化（ADR-002 扩展）

- chunk fingerprint 新增 3 个字段（**仅当 `separate_vocals=True` 时才 append**，
  否则 False 路径的旧 hash 保持不变，旧 chunk 全部复用）：
  - `separate_vocals: bool`
  - `vocal_sep_backend: str` = `"demucs"`（预留未来切 MDX-Net）
  - `vocal_sep_model: str` = `"htdemucs"`（模型名，htdemucs / htdemucs_ft）
  - `vocal_sep_input_hash: str` = `sha1(input_path + filesize + mtime)[:8]`
    （防止视频文件被替换后复用脏 vocals.wav）
- 分离输出持久化：`{base}.{vocal_fp}.vocals.wav`。下次重跑时：
  `input_hash` + 后端参数匹配 → **直接跳过分离**（零开销，断点续跑友好）。
- 文件落地于 `outdir`（即视频所在目录），与 `chunk_N.json` 同生命周期。

### 5. 8GB GPU 显存分步调度

8GB 卡上 `htdemucs` (~2GB) + `large-v3 int8_float16` (~5.5GB) 同驻会 OOM。
因此 `transcribe_video` 内部执行顺序**强制串行**：

```
┌──────────────────────────────────────┐
│ 1. demucs.separate(input)            │  → 加载 demucs 模型
│    → 写 {base}.vocals.wav            │
│    → **显式 del demucs_model + GC**  │  → 释放！
├──────────────────────────────────────┤
│ 2. 加载 WhisperModel(large-v3)       │  → 此时只有 Whisper 占显存
│    → 正常 chunk 循环                 │
└──────────────────────────────────────┘
```

若未来需要支持「多任务并发」，该调度封装成 `@contextmanager` 即可。当前单任务 CLI
不需要。CPU 环境下该顺序无副作用。

### 6. `fill_gaps` 恢复路径一致化（ADR-016 2a 对齐）

当主转写使用了 vocals.wav，`fill_gaps._decode_once` 的强制解码
**必须从同一份 vocals.wav 中切窗口**，而不是回落到原视频。
否则「主转写用干净人声、恢复解码用带噪原音」会导致 decode 配方不一致，
在噪声窗口仍然空转漏检。

`resegment --windows` 的局部重转写同样支持该 flag，语义一致。

## Consequences

- **新增文件** `src/video_translate/vocal_sep.py`：demucs API 封装、指纹、
  缓存跳过、显存释放。
- **新增文件** `tests/test_vocal_sep.py`：
  - 纯函数单测：指纹、缓存命中跳过逻辑（不需要真跑 demucs）；
  - Mock 单测：demucs 未安装时优雅降级分支；
  - 断言测试：Demucs 输出长度 = 输入长度（铁律断言）。
- **修改** `transcribe.py`：`transcribe_fingerprint()` 扩维度；
  `transcribe_video()` / `transcribe_window()` 增加 `separate_vocals`
  参数并接入 audio_source 切换逻辑。
- **修改** `fill_gaps.py`：`fill_gaps()` / `_decode_once()` 新增
  `audio_source: str | None` 透传。
- **修改** `config.py`：`Config.separate_vocals: bool = False`，
  环境变量 `VT_SEPARATE_VOCALS`。
- **修改** `cli.py`：`transcribe`/`run`/`resegment` 加 `--separate-vocals` flag；
  `cmd_doctor` 打印 demucs 可用状态；优雅降级告警。
- **修改** `pyproject.toml`：新增 `[audio]` optional-deps = `["demucs"]`
  （**已于 2026-08-21 改为核心 `dependencies`，见 §3 变更记录**）。
- **文档**：`README.md` CLI 速查 + 环境变量表；`AGENTS.md` Phase 0 补充
  「强 BGM → 建议 --separate-vocals」；`TOOLCHAIN.md` 加
  `pip install -e .` 说明（demucs 现为默认安装）。
- **验收标准**（MAJOR_VERSION_PLAN §5 T2）：
  `--separate-vocals` 成功分离 vocals.wav 喂给 Whisper，强 BGM 场景
  幻觉数显著下降，时间戳范围与未开 flag 一致（无全局偏移）。

## Known Limitations

- Demucs CPU 路径非常慢（~10x 实时）；未装 GPU 时 doctor 打印显式提示
  「建议 GPU 运行，CPU 可能 10x+」，但用户显式开了 flag 就坚持跑（不替用户做决策）。
- `htdemucs_ft` 精细版模型 ~100MB 更大、慢约 2x；留作
  `--demucs-model htdemucs_ft` 隐藏高级参数，默认不用。
- 人声分离只清洗「Whisper 的输入」，不参与声学层 `verify` 的独立参照计算——
  `silencedetect` / `volumedetect` 永远从**原视频**抽 profile（ADR-012 不变量：
  独立参照必须是原始未处理的声学事实，不能是清洗后的派生音轨，否则自证循环）。