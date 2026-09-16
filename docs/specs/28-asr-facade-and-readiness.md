# Spec 28 — ASR 层抽离 · 第二步（① 层门面 + Provider 就绪接线）

- 状态：批准（已实现）
- 日期：2026-09-16
- 关联：[ADR-038](../adr/038-asr-layer-extraction.md)（决策）、[Spec 27](27-asr-provider.md)（第一步 · 接口契约）、[Spec 02](02-transcribe.md)（转写行为）、ADR-030（控制面；原 CONTROL-PLANE-PLAN 已归档至 [`docs/archive/`](../archive/)）

## 范围

- **IN（第二步，已落地）**：
  - **A 块**：`asr.py` 门面 `run_asr()` —— 把原先散落在 `cli.cmd_transcribe` 的 ① 层
    编排（人声分离 → 引擎转写 → 幻觉过滤 + 合并 + 切分 → 漏音补洞 + review）收进 ① 层；
    `cmd_transcribe` 退化为薄壳（前置检查 + 参数归一化 + 控制面记账 + 退出码映射）。
  - **B 块**：**Provider 自报就绪接线**（ADR-038 D7）——能力分通用 / 引擎特定，
    阶段表只声明通用前置，引擎特定前置由 Provider 的 `prerequisites()` 自报并在组装
    ctx 时注入；doctor 的模型体检项改为按 Provider 自报渲染；`model:<name>` 能力 id
    由前缀解析；模型缓存探测下沉为单一来源（解 `capabilities` → `cli` 的循环依赖）。
- **OUT（更远）**：引擎阈值画像；第二个 Provider 实现；执行顺序调整（切分与补洞，
  见 ADR-038「已知问题」，属**行为变更**，须单独评估与回归）。

## 不变量（load-bearing）

1. **产物零变化**：`segments_raw.json` / `segments_en.json` 的字段、形状与**内容**不变
   （ADR-035 契约表仍是唯一事实来源）。A 块搬迁经实弹冒烟逐字节比对；B 块不触碰产物。
2. **① 层门面不做控制面记账**：`run_asr` 不读写 `vt_state`、不决定退出码——那是 cli 层
   的事（ADR-038 D2/D4）。因此 `AsrRequest` / `AsrOutcome` 均为纯数据。
3. **门面不接收 `argparse.Namespace`**：cli 负责把 Namespace 归一化成 `AsrRequest`，
   避免 `asr.py` 反向耦合入口壳。
4. **Provider 边界 = 音频 → 原始段**：合并 / 补洞等方案后处理归 ① 层编排，不塞进
   Provider（换引擎时这些逻辑复用，仅阈值需重标定）。
5. **通用前置是引擎无关的**：`ffmpeg` / `ffprobe` 是所有引擎的共同前置（抽音频、测静音、
   探时长），且服务 verify 的独立验证——故必须与引擎解耦。
6. **显式意图硬停必须冒泡**：`--separate-vocals` 等 EXPLICIT 请求无法满足时抛 `GateFail`
   → exit 8，不得被兜底吞成 `EXIT_RUNTIME`（裁决一）。

## 接口契约

### `run_asr(request, *, provider=None, silence_intervals=None, progress=print) -> AsrOutcome`

编排顺序（与搬迁前逐项等价）：

1. 人声分离（T2）—— **必须先于任何 Whisper import / 构造**（8GB 显存串行调度）
2. 引擎转写（Provider；含 T4 强制对齐）
3. 幻觉过滤 + 断句合并 + 切分 + 短句合并 + 孤儿并右（`merge.apply_merge`）
4. 漏音补洞 + review + G1/G2/G3（`fill_gaps.fill_gaps`）
5. **全大写伪影归一化**（`transcribe._normalize_caps`）—— 必须在 3/4 **之后**，
   因为这两步都会重写段文件，早于它们运行会被覆盖（见 [HISTORY V15](../HISTORY.md)）。

- `silence_intervals` 由调用方供给（state 读写属控制面，留在 cli 层）；为 `None` 时
  门面兜底重算一次，保证直调 `run_asr` 也拿得到独立参照（ADR-012）。
- `provider` 默认 `FasterWhisperProvider`（换引擎只换它）。

### `AsrRequest` / `AsrOutcome`

纯数据（frozen dataclass）。`AsrRequest` 承载引擎参数（`TranscriberConfig`）+ 后处理
参数与开关（merge / split / snap_drift / audit / review / g3 / allow_degrade）；
`AsrOutcome` 回传段路径 / 段数据 / 检测语言 / 人声分离产物（`audio_source` 等）。

### 能力分层与自报就绪（ADR-038 D7）

- `capabilities.Capability.common`：**通用前置**标记。当前通用集合 = `{ffmpeg, ffprobe}`
  （`capabilities.common_cap_names()`）。
- `pipeline_def.STAGES["transcribe"]["caps"]` **只声明通用前置**；引擎特定前置由
  `asr.engine_prerequisites(provider=None)` 自报，`pipeline.build_ctx` 注入
  `ctx["_engine_caps"]`，`pipeline.check_stage` 合并检查两者。
- **注入时只取 `core=True` 的硬前置**（当前 = `model:large-v3`）：`prerequisites()`
  是「本引擎**可能用到**哪些能力」的清单，其中 `cuda` / `whisperx` / `demucs` 属**可选**
  ——CPU 可跑、`--align auto` 会自动降级 `none`、demucs 仅 `--separate-vocals` 需要。
  若把它们当作「阻断本阶段」的问题，CPU / macOS 机器的 NEXT 块会长期挂着误导性 MISS。
  可选能力的**体检**归 doctor（走未过滤的 `engine_prerequisites()`）。
- `capabilities.capability(name)` 对未注册的 `model:<name>` 型 id **按前缀解析**
  （探测复用 `model_cache` 的 E3 完整性口径）——新引擎声明 `model:sensevoice`
  无需改动 `capabilities.py`。
- doctor 的模型体检项按 Provider 自报渲染（不再写死 `large-v3`）；`cuda` / `whisperx`
  / `demucs` 仍由各自的详细段承担，不重复列举。

### 模型缓存单一来源（`model_cache`）

`REPO_ROOT` / `LOCAL_MODEL_DIR` / `hf_cache_dir()` / `MODEL_MIN_BYTES` /
`model_cached()` / `find_incomplete_model_bins()` / `resolve_model_path()` 收敛到
`model_cache.py`，由 `cli` / `transcribe` / `vocal_sep` 共同消费。此前三处各自推导
`<repo>/models`，且 `capabilities` 需反向 import `cli`（靠延迟 import 绕过循环）。

## TDD 清单

- `tests/test_asr_facade.py`：编排顺序 / merge・audit 开关 / 后处理阈值透传 /
  `audio_source` 透传 / 不触碰控制面 / 返回载荷 / 全大写伪影末端落盘接线；
- `tests/test_asr_provider.py`：`prerequisites()` 的 id 全部存在于 `capabilities.CAPS`；
- `tests/test_capabilities.py`：通用集合只有 ffmpeg/ffprobe；`model:<name>` 动态解析
  （未注册可探测、可取指引、未就绪降级为 False）；未知非 model id 仍报错；
- `tests/test_pipeline.py`：`STAGES["transcribe"]["caps"]` 收窄；`build_ctx` 注入
  `_engine_caps`（含 provider 覆盖与异常降级）；`check_stage` 合并检查注入项；
- `tests/test_model_cache.py`：完整性口径（本地 / HF 快照 / 截断）+ `resolve_model_path`。

## 验收标准

1. 全量 `uv run pytest` 绿，用例数只增不减。
2. `segments_raw.json` / `segments_en.json` 逐字节不变（A 块实弹冒烟已验证）。
3. 换引擎场景：新增一个 Provider（含自己的 `prerequisites()`）**不需要**改
   `pipeline_def.STAGES`、`capabilities.py` 的控制面逻辑或 doctor 的渲染结构。
