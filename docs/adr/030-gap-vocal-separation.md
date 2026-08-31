# ADR-030 — 大空档人声分离召回（Gap Vocal Separation Recovery）

- 状态：接受
- 日期：2026-08-30
- 关联：ADR-012（声学铁律）、ADR-016（fill_gaps 召回网）、ADR-017（人声分离预处理）、ADR-021（恢复段幻觉守卫）、Spec 24、`gap_vocal_sep.py`

## 背景

即使全局开启 `--separate-vocals`（ADR-017），faster-whisper 仍可能把“被笑声/欢呼/强 BGM 压住的真实语音”误判为 non-speech 或合并到相邻段里。`fill_gaps`（ADR-016）的裸跑重解码能召回一部分，但当噪声能量显著高于人声时，直接从原视频切窗口解码仍会把真实语音漏掉。

在 `Nobody Can Handle Christopher Walken's STRANGE Hum.mp4` 的实战验证中：

- 332–352s（5:45 附近）：约 23 秒的真实对白被整段漏掉，原视频直接解码几乎空白；
- 对同一窗口先做 Demucs 人声分离，再从干净人声解码，可稳定恢复出 2–4 条真实语句；
- 377–388s、821–833s 等窗口虽然也是“大空档 + 高能量”，但分离后得到的是笑声幻觉或 BGM 歌词，必须被下游过滤器拦截。

因此需要在 `fill_gaps` 内部增加一个可选的“硬洞人声分离召回”层：只对那些“既大又响”的洞临时跑 Demucs，用更严格的质量门筛选结果。

## 决策

### 1. 独立 opt-in，默认关闭

新增 CLI flag `--gap-vocal-sep` 与配置字段 `gap_vocal_sep: bool = False`。它**不跟随**全局 `--separate-vocals`，原因：

- 全局 `--separate-vocals` 已产出 `vocals.wav` 时，`fill_gaps` 本来就会从 vocals 解码，不需要再分；
- 按洞跑 Demucs 是昂贵的（GPU 时间），不应默认开启；
- 用户可以只在“某条视频明显漏句”时打开，而不改变默认流水线。

### 2. 窗口级 Demucs，串行显存调度

在 `fill_gaps` 加载 WhisperModel 之前，先完成 hole/collapse 检测并识别出 hard gaps；若存在 hard gaps 且全局 vocals.wav 不可用，则逐个（或批量）对窗口跑 Demucs 分离，生成临时 `vocals.wav` 映射，随后释放 Demucs 占用的 GPU 内存，再加载 Whisper 解码。

这遵守 ADR-017 §5 的“Demucs 与 Whisper 不得同驻 8GB GPU”铁律。

### 3. 硬洞识别标准

候选洞必须同时满足：

- 长度 ≥ `gap_vocal_sep_min_gap`（默认 5.0 s）
- 窗口内音量不低：mean dB > `gap_vocal_sep_energy_mean_db`（默认 -30 dB）或 max dB > `gap_vocal_sep_energy_max_db`（默认 -10 dB）
- 未被 silencedetect 判为纯静音（复用 `fill_gaps` 已有的 silence filter）

高能量条件用于避免对静音洞浪费算力；silencedetect 条件用于避免对片头/片尾/真实停顿强制解码出幻觉。

### 4. 严格后过滤

从 gap vocals 解码出的恢复段要经过比常规恢复段更严的幻觉守卫 `_is_gap_vocal_hallucination`：

- `no_speech_prob >= 0.5` 丢弃（比常规 0.6 更严）
- `avg_logprob < -0.8` 丢弃（比常规 -1.0 更严）
- 保留 ADR-021 信号 A/B/D：重叠、语速、零时长词
- 新增简单歌词/重复模式信号：短窗口内同一 bigram 重复 2 次及以上，或首尾 token 相同且 `no_speech_prob` 偏高

该守卫专门拦截 821s 类“强 BGM 下 Demucs 仍残留歌词”的幻觉。

### 5. 复用全局 vocals.wav（只解码该洞窗口）

若调用方传入 `audio_source` 且该文件存在（即全局 `--separate-vocals` 已启用），则跳过窗口 Demucs，直接把 `audio_source` 作为对应硬洞的解码源。这保证全局分离与按洞分离两条路径的结果一致。

**只解码该洞对应的窗口，绝不解码整条人声轨。** 两条来源的长度语义不同：

- 窗口级 Demucs 的产物只含该洞，洞在文件内的起点是 `0.0`；
- 全局 vocals.wav 是**全长**轨（等于视频时长），洞在文件内的起点是它自己的绝对偏移 `gs`。

因此 `recover_hard_gaps` 返回的映射值是 `(vocals_path, local_start)` 二元组，`_decode_gap_vocals` 按 `(local_start, ge-gs)` 切窗解码。

> 实现教训：早期版本直接对 `vocals_path` 全长解码（`extract_chunk(path, wav, 0.0, probe_duration(path))`）。窗口级路径下恰好等价，但复用全局轨时**每个洞都要跑一遍整片转写**——实测一次跑 1 小时无任何输出，且恢复出的句子被 `+gs` 平移后时间戳全错（例如把 366–406s 的台词安到 34.6s）。长度语义必须显式携带，不能靠"文件里应该只有这个洞"的隐含假设。

## 不变量

- **默认路径字节级不变**：`gap_vocal_sep=False` 时，`fill_gaps` 行为与修改前完全一致。
- **声学铁律**：临时 vocals.wav 必须与原洞窗口时长一致（±0.05s），否则丢弃该洞。
- **显存串行**：Demucs 运行期间 WhisperModel 不得驻留 GPU；解码期间 Demucs 模型不得驻留 GPU。
- **只删除不重算时间戳**：恢复段时间戳来自 Whisper 解码，仅按窗口起点平移。
- **临时文件不污染工作目录**：窗口 Demucs 输出放在系统临时目录，解码后清理。

## 后果

- 新增 `src/video_translate/gap_vocal_sep.py`：硬洞识别、窗口 Demucs、严格过滤。
- 修改 `src/video_translate/fill_gaps.py`：在合适位置调度 gap vocal separation，把硬洞从普通 hole 路径里摘除。
- 修改 `src/video_translate/cli.py` 与 `src/video_translate/config.py`：新增 flag、配置字段、env 映射。
- 新增 `tests/test_gap_vocal_sep.py`：TDD 测试覆盖硬洞选择、能量计算、严格过滤。
- 新增 `docs/adr/030-gap-vocal-separation.md` 与 `docs/specs/24-gap-vocal-separation.md`：本 ADR 与详细行为规范。

## 已知限制

- 按洞逐个跑 Demucs 在 CPU 上很慢；当前默认只处理 1–5 个硬洞，可接受。
- 歌词/重复模式守卫是简单启发式，未来可被语义回读或 whisperX 对齐替代。
- 阈值（-30 dB mean / -10 dB max）从单视频标定，后续需在更多素材上回归。
