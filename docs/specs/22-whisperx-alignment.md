# Spec 22: WhisperX 强制声学对齐（词级时间戳精修）

- 状态: 批准（实现）
- 日期: 2026-08-28
- 修订: 2026-08-29（T4 默认化：`--align` 默认 `none` → `auto`）
- 关联: ADR-028、ADR-013、MAJOR_VERSION_PLAN §T4

## 目标

转写完成后新增一道**时间轴精修工序**：用声学强制对齐把每个词的起止时间
校准到真实发音，消除快语速 / 长台词场景下「字幕抢跑 / 滞后」的观感问题。
**默认 `auto`（T4 默认化）**：具备高性能计算条件的 GPU 环境自动启用 whisperx，
其余环境自动回退 `none` 并打印提示，主流程绝不中断。

## 范围

- **IN（本 Spec）**：`transcribe` 与 `run` 命令的 `--align {auto,none,whisperx}` 开关；
  词级时间戳精修；独立分块对齐缓存（断点续跑）；环境自检状态行；优雅降级。
- **OUT（未来阶段）**：`resegment` 命令暂不接对齐（其自身重转写窗口不在本流水线
  对齐 pass 内，保持现状）；说话人分离属 T5；其他对齐后端（stable-ts）预留接口但
  本阶段只实现 `whisperx`。

## 配置（三级覆盖）

与项目既有 Config 体系一致（参考 ADR-027 / Spec 21 的 style 接入方式）：

| 级别 | 形式 | 示例 |
|---|---|---|
| CLI | `--align {auto,none,whisperx}` | `--align whisperx` |
| 环境变量 | `VT_ALIGN` | `VT_ALIGN=whisperx` |
| toml | `[transcribe] align = "whisperx"` | 见 `config.py` toml sections |
| Python 字段 | `Config.align` | 默认 `"auto"` |

覆盖优先级：CLI `--align` > `VT_ALIGN` > toml `[transcribe].align` > 默认 `"auto"`。
非法值（非 `auto`/`none`/`whisperx`）→ 告警并回落 `"auto"`（不崩溃）。

## 行为契约

### 1. 对齐开关与默认值

- 默认 `auto`（T4 默认化）：**CUDA + whisperx 可用** → 启用 wav2vec2 强制对齐；
  否则（macOS / 未装 / 无 CUDA）→ 打印提示并回退 `none`（行为与现状逐字节一致）。
- 显式 `--align none`：流水线行为与现状逐字节一致（golden 保护）。
- 显式 `--align whisperx`：强制启用；不可用时告警并回退 `none`。

### 2. 词级时间戳精修（不变量，铁律 1）

- 对齐**仅**修正每个词的 `start` / `end`，以及由其推导的段落 `start` / `end`。
- 以下**一律不变**：`text`、段数、段落顺序、断句分组、置信度字段
  （`avg_logprob` / `no_speech_prob` / `compression_ratio`）。
- merge.py 按词戳断句的下游逻辑（`_emit` / `_split_by_gap` / `snap_drifted_words`）
  全链路受益，无需改动。

### 3. 优雅降级矩阵（铁律 2）

**默认 `auto`**：当以下任一条件成立时，自动回退 `none` 并打印**提示**（非告警，
因为是默认路径）：
- 当前为 **macOS**（无 CUDA，whisperx 对齐需 NVIDIA GPU）；
- **无 CUDA**（`torch.cuda.is_available()` 为 False）；
- **whisperx 包未安装**（未 `uv sync --extra gpu`）；

**显式 `--align whisperx`**，但上述任一条件成立时：
- 打印 stderr **告警**（含原因与修复指引），**自动回退 `none` 继续执行**，退出码
  保持 `EXIT_OK`，主流程绝不中断。

另：转写语言**无对应 wav2vec2 模型**时，对齐模型加载失败会被 `align_segments`
捕获，该段保留 DTW 词戳并打印告警（逐段安全回退，ADR-028 决策 4）。

### 4. 断点续跑（铁律 3）

- 对齐产物为**独立缓存层**，不污染转写 chunk 缓存：
  `{outdir}/{base}.{transcribe_fp}.chunk_{ci}.whisperx.json`
  其中 `transcribe_fp` 为既有转写指纹（不含 align 维度，见 ADR-028 决策 2）。
- 重跑时：已存在对齐缓存的块跳过；缺失块补跑；切换 `--align` 模式仅重跑对齐
  pass，复用既有转写 chunk 缓存（不触发重转写）。
- 对齐 pass 在转写循环**之后**、`merge_chunks` **之前**执行（ADR-013 定位）。

### 5. 显存分步调度（8GB 红线）

- 转写循环结束 → 显式卸载 Whisper（`del model` + `gc` + `torch.cuda.empty_cache`）→
  加载 wav2vec2（仅一次，复用全部 chunk）→ 对齐完再次清理。
- Whisper 与 wav2vec2 不同时驻留 8GB 卡。

### 6. 逐段安全回退

- 某段对齐返回词数与原段词数不一致 → 该段保留 DTW 词戳 + 告警，其余段正常回写，
  绝不整批失败。

### 7. 环境自检

- `doctor` 命令新增 whisperx 状态行：
  - 已安装且在 GPU 环境 → `whisperx: OK — GPU alignment available (--align whisperx)`
  - 未安装 → `whisperx: not installed — GPU alignment unavailable; install: uv sync --extra gpu`
  - macOS → 提示该平台不支持对齐（即使已安装也不生效）。

## 依赖与准入（R6 五项清单）

| 项 | 评估 |
|---|---|
| 跨平台降级 | macOS / 默认安装零新依赖；`[gpu]` extra 仅 Windows/Linux+CUDA（铁律 2） |
| 指纹影响 | 对齐**不**进转写指纹；独立对齐缓存文件名嵌后端隔离（ADR-028 决策 2） |
| 8GB 显存预算 | 分步加载（决策 3），不起冲突 |
| 等价实现评估 | whisperx 冲突时兜底用 `transformers` wav2vec2（torch 已为核心依赖） |
| lockfile 同步 | pyproject 变更与 `uv.lock` 同 commit（R3） |

## TDD 验收清单

- [ ] `whisperx_available()`：mock import 成功 / 失败分别返回 `True` / `False`；
      macOS 平台探测返回 `False`。
- [ ] `align_fingerprint`：相同输入稳定；后端变化时不同；与转写指纹解耦。
- [ ] `align_segments` 不变量：text / 段数 / 顺序 / 分组不变，仅 `words[].start/end`
      被改写；原时间戳单调保留。
- [ ] `align_segments` 词数不匹配：mock 返回不同词数 → 该段保留原词戳 + 告警，
      其余段回写。
- [ ] 显存清理：mock `torch.cuda` 验证 `del` + `empty_cache` 在对齐前后被调用。
- [ ] 降级：显式 whisperx + 库缺失 / macOS → `transcribe_video` 不出错，回退 none，
      退出 `EXIT_OK`。
- [ ] 配置三级覆盖：CLI > 环境变量 > toml > 默认；非法值回落 auto。
- [ ] 对齐缓存写入与二次复用：第一次写 `{base}.{fp}.chunk_{ci}.whisperx.json`，
      第二次命中跳过。
- [ ] **golden 回归**：`--align none` 路径与现状字节级一致（转写指纹不变）。
