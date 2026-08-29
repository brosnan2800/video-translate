# ADR-028: WhisperX 强制声学对齐 — 实现级决策

- 状态: 接受（实现）
- 日期: 2026-08-28
- 上游: ADR-013（WhisperX 强制对齐，2026-08-18，决策已接受，实现延后至本 ADR）
- 关联: MAJOR_VERSION_PLAN §T4、Spec 22

## 背景

ADR-013 已固化高层决策：引入词级强制声学对齐修复极端语速下的时间戳漂移。
本 ADR 落地 **实现级** 决策，解决 ADR-013 未细化的五个工程问题：
1. 是否替换转写核心（faster-whisper 1.2.1）？
2. 对齐工序在流水线中的位置与缓存形态？
3. 8GB 显存红线下如何调度 Whisper 与 wav2vec2？
4. 对齐返回的逐词结果与原段不匹配时如何安全回退？
5. whisperx 依赖如何隔离，且不破坏 Mac / 锁文件确定性？

## 决策

### 决策 1 — 只借用对齐，不换核心

**仅 import whisperx 的对齐能力**（`load_align_model` + `align`），转写仍走
`faster-whisper==1.2.1` 钉版。这是 ADR-013「仅对齐不换核心」回退预案的**正向设计**，
而非失败后的降级。

理由：
- whisperx 的转写路径会拉入自有（可能更新的）faster-whisper / ctranslate2，
  与项目钉版冲突（R6 风险）。只借对齐模块，冲突面最小。
- 转写核心已在 E4/T2 等阶段高度打磨（VAD、beam、no_speech、人声分离），
  不应为对齐能力冒然替换。

### 决策 2 — 独立对齐 pass + 独立缓存层（不追加进转写指纹）

ADR-013 字面建议「缓存指纹必须含 align 后端（与 device/compute_type 同列）」。
本 ADR 改为：**不把 align 追加进 `transcribe_fingerprint`**，而是把对齐产物做成
**独立缓存层**，理由有三：

1. **避免重转写**：切换 `--align` 模式若改变转写指纹，会触发全量重新转写
   （GPU 最贵的一步，2 小时视频约 1–2 小时）。对齐本身便宜（wav2vec2，仅一次
   模型加载），应可单独重跑。独立缓存层下，切换对齐模式只重跑对齐 pass，
   复用既有转写 chunk 缓存（铁律 3「断点续跑」受益）。
2. **golden 字节兼容**：`--align none` 路径的转写指纹与历史哈希完全一致，
   T3/T2 的所有 golden 测试零回归。
   **注意（2026-08-29 T4 默认化后）**：默认已改为 `auto`，GPU 主机默认即走
   whisperx；上述字节级一致仅对**显式 `--align none`**，或 `auto` 降级为 `none`
   的主机（macOS / 无 CUDA / 未装 whisperx）成立。
3. **同样实现「不同对齐模式产物隔离」**：对齐缓存文件名直接嵌入后端，
   天然隔离（`{base}.{fp}.chunk_{ci}.whisperx.json`）。未来新增后端（如 stable-ts）
   零冲突。

对齐 pass 位于 **chunk 转写循环之后、merge_chunks 之前**（ADR-013 定位），
逐 chunk 执行：重抽该块音频 → wav2vec2 对齐 → 回写词戳（含 cstart 偏移）→
写独立对齐缓存。断点续跑：已存在对齐缓存的块跳过。

### 决策 3 — 分步执行守 8GB 显存红线

转写循环结束 → **显式卸载 Whisper**（`del model` + `gc.collect()` +
`torch.cuda.empty_cache()`，沿用 T2 的 `_vocal_sep_step` 显存清理模式）→
加载 wav2vec2 对齐模型（约 1.2GB，仅加载一次复用全部 chunk）→ 对齐完再次清理。
绝不让 Whisper（~3GB）与 wav2vec2（~1.2GB）同时驻留 8GB 卡。

### 决策 4 — 逐段安全回退

whisperx `align` 偶发会对某段重新分词（词数与原段 `words` 数不一致）。此时：
- 该段**保留** DTW 词戳（更快 whisper 的原始词级时间），仅打印告警；
- 其余段正常回写对齐结果；
- 绝不整批失败、绝不动文本/断句（铁律 1）。

语言无对应 wav2vec2 模型（如某些小语种）→ 整批告警跳过对齐，回退 DTW 词戳。

### 决策 5 — 依赖隔离与准入

- pyproject 新增 `[gpu]` 可选依赖组：`whisperx` 钉版本（**`whisperx==3.8.6`**，
  选型理由与 3.8.x 的 API 契约见决策 6）。Mac / 默认 `uv sync` **零新依赖**（铁律 2）。
- 仅 Windows / Linux + CUDA 环境安装：`uv sync --extra gpu`。
- R3：pyproject 变更与 `uv.lock` 更新在**同一 commit**。
- R6 五项准入清单（跨平台降级 / 指纹影响 / 8GB 显存预算 / 等价实现评估 /
  lockfile 同步）写入 Spec 22。
- 冲突兜底：若 whisperx 与 1.2.1 在 uv 下无法共存，按 ADR-013 预案降 whisperx
  版本或改用 `transformers` 的 wav2vec2 直接实现（torch 已是核心依赖）。

### 决策 6 — whisperx 3.8 的实际 API 契约（2026-08-28 实测补遗）

决策 5 原钉的 `whisperx==3.3.6` 在本项目**不可用**：其官方元数据为
`requires_python: >=3.9,<3.13`，而项目 `.python-version` 钉的是 **3.13.12**；
且它在 lock 中锁死 `ctranslate2==4.4.0`（仅 cp310–cp312 wheel，无 cp313）。
已升级至 **`whisperx==3.8.6`**（`requires_python >=3.10,<3.14`、
`ctranslate2>=4.5.0`），并连带把 torch 从 2.6.0+cu124 升到 **2.8.0+cu128**
（CUDA 索引 `pytorch-cu124` → `pytorch-cu128`）。两个 PyTorch 索引均标记
`explicit = true`，否则它们会用陈旧版本遮蔽 PyPI 上的 tqdm 等通用包，导致解析
无解。3.8.2 / 3.8.3 / 3.7.3 已被上游 yanked（词时间戳缺陷 / torch 不兼容），
必须 ≥3.8.4。

3.8.x 的 `align()` 与 3.3.x 有四处**契约差异**，每一处都会让对齐「看起来成功、
实际一个时间戳都没改」，必须按下表对接：

| # | 3.8.x 的实际行为 | 不做处理的后果 | 对策 |
|---|---|---|---|
| 1 | segment 必须携带 `start`/`end`（用于切音频窗口） | 只传 `text`+`words` → 深处 `KeyError('start')` | 传入 chunk-local `start`/`end` |
| 2 | 按句子边界**重新切分**输入段（3 段进 → 5 段出） | `zip(输入段, aligned["segments"])` 全部错位 → 静默回退 DTW | 不读 `aligned["segments"]`，改走扁平的 `word_segments` |
| 3 | 词流与 faster-whisper DTW **非 1:1**（本次 245 → 243，`white -collar` 被合并成 `white-collar`） | 位置消费一旦漂移，后续全错 | **逐段对齐**：差异只影响那一段，它保留 DTW，其余照常精修 |
| 4 | `word_segments` 按时间排序，而我们的段序**不保证**按时间（`fill_gaps` 把补洞恢复段追加在末尾，时间戳却在更早的窗口内） | 顺序消费必然错位 | 逐段对齐天然免疫（每段独立取词） |

代价是 N 次 `align()` 调用而非 1 次（本次 24 段），但 wav2vec2 对单段的前向开销
极小，且它把「分词差异」这一不可控因素隔离在单段内 —— 这正是决策 4「逐段安全
回退」想要的粒度。词形比对需容忍 whisperx 的文本规范化（去前导空格、折叠弯引号
与破折号，`_norm()` 负责）。

**NLTK 语料（新增外部资产）**：whisperx 对齐前用 nltk 做句子切分，需要 `punkt`
/ `punkt_tab`。缺失时 `align()` 抛 LookupError，被决策 4 的回退吞掉 → 对齐**静默
失效**。按 R4/R5 处理：`video-translate setup --align` 下载到
`<repo>/models/nltk_data`（幂等、gitignore、零 C 盘）；`align.py` 在对齐前把该目录
注册进 `nltk.data.path`，因此运行时**不需要** `NLTK_DATA` 环境变量；`doctor` 在
缺失时打印 `video-translate setup --align` 修复命令。

**实测（比尔盖茨样本：清晰录音室演讲，24 个 chunk 段 / 430 个词边界）**：22 段
成功对齐，2 段因 `white-collar` / `blue-collar` 分词合并保留 DTW；词边界平均修正
**5.4 ms**、中位数 2 ms、最大 **323 ms**（≥20ms 的 28 条、≥150ms 的 1 条）。
即：干净样本的 DTW 本来就准，对齐收益集中在少数漂移点上 —— 验收仍以本 ADR 针对的
「鲍德温类漂移样本」为准（见 §验收）。

## 不变量

1. **声学真值不可篡改**（铁律 1）：对齐只改 `words[].start/end` 与由其推导的
   段落 `start/end`；`text`、段数、顺序、断句分组、`avg_logprob` 等置信度字段
   **一律不变**。
2. **音源一致**：对齐抽取音频与转写同一音源（`_extract_src`：人声分离开启时
   用 `vocals.wav`，时间轴经 ADR-017 时长不变量锚定原片）。
3. **语言传递**：对齐所用语言取转写 `info.language`（自动检测结果），不重新检测。
4. **零回归保护**：显式 `--align none` → 流水线行为与现状逐字节一致（golden 回归）。
   默认 `auto`（2026-08-29 修订 / T4 默认化）：GPU 环境自动启用 whisperx，其余环境
   回退 `none` 行为不变。

## 影响面

| 文件 | 变更 |
|---|---|
| `src/video_translate/align.py` | 新增：模块（探测 / 指纹 / 对齐 / 显存清理） |
| `src/video_translate/config.py` | `align` 字段 + `VT_ALIGN` + toml `[transcribe].align` |
| `src/video_translate/transcribe.py` | `transcribe_video` 新增 `align_backend` 参数与对齐 pass |
| `src/video_translate/cli.py` | `transcribe`/`run` 加 `--align`；`doctor` 加 whisperx 状态行；降级告警 |
| `pyproject.toml` | 新增 `[gpu]` extra（whisperx 钉版） |
| `uv.lock` | 同 commit 重跑（R3） |
| `docs/adr/013-whisperx-gpu-forced-alignment.md` | 状态：延后 → 已由 ADR-028 落地 |
| `docs/specs/22-whisperx-alignment.md` | 新增行为契约 |
| `TOOLCHAIN.md` / `README.md` / `AGENTS.md` / `MAJOR_VERSION_PLAN.md` | 同步 |

## 验收

- pytest 全绿（Mac mock，无需 GPU / 库）。
- `--align none` 路径与现状字节级一致（golden）。
- GPU 盒：鲍德温类漂移样本对齐后时间戳误差 < 150ms（`verify --video` 声学 lane 量化留档）。
