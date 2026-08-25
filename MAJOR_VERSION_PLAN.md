# V5 开发计划：CUDA / Windows 部署 + 翻译与声学衍生能力演进

> 状态：**阶段一已落地，全面升级修订版**
> 创建：2026-07-31
> 修订：2026-08-21（完成 T1 CUDA+.env 工具链隔离；引入双轨翻译风格与智能人声分离预处理；扩展独立 LLM API 与 Web 校对看板）
> 修订：2026-08-25（对标研究 [Voice-Pro](docs/RESEARCH-voice-pro.md) 后新增 **E1-E4 环境确定性工程**为下一阶段最高优先级；固化「依赖与外部工具管理规则」§3.2；原 T3-T7 顺延）
> 目标分支：`feat/v5-cuda-windows`
> 当前版本：`4.0.0` $\rightarrow$ 目标版本：`5.0.0`

> **执行模型交接说明**：本文档是任务开发的唯一事实来源。E1-E4 为自包含任务（背景/动作/涉及文件/验收标准俱全），可直接执行无需额外上下文。执行前必读 [AGENTS.md](AGENTS.md) §1 红线表与本文档 §3.2 依赖规则。

---

## 0. 背景与核心铁律

### 0.1 现状与已完成成果（已核对代码库）
- **T1 阶段成果（已全部落地）**：
  - **CUDA 与设备抽象（ADR-014）**：实现 `resolve_device()`，支持 `VT_DEVICE=auto`（GPU 命中 `cuda/int8_float16`，Mac/无卡平滑回退 `cpu/int8`）。
  - **工具链环境隔离（`toolchain.py`）**：建立 `.env` / `.env.win` / `.env.mac` / `.env.linux` 分层加载机制，自动探测并注入 `VT_FFMPEG_DIR` 和 `VT_CUDA_DIR`，彻底消除手动 `$env:PATH` 注入与单机路径污染。
  - **文档架构规范化**：`README.md` 与 `AGENTS.md` 完成重构，确立「避坑防呆红线速查表」与标准五阶段状态机；外部工具链指引收敛至 `TOOLCHAIN.md`。
- **主线质量护栏状态**：
  - ADR-011 / ADR-012：VAD 默认裸跑，`doctor --video` 音频画像自动路由 VAD；统一三 Lane 门禁 `verify`。
  - ADR-015 / ADR-016：`--adaptive-vad` 按 chunk 动态路由 VAD；`fill_gaps` 裸跑恢复网与 `uncovered-audio` 漏检探测；语义回读默认开启。
  - **ADR-020（已落地）**：尾部回音幻觉防御。`drop_hallucination_segments` 新增第四信号（段窗口被邻居时间窗包含且含零时长词，确定性，可区分边界模糊）与第五信号（Whisper 低 `avg_logprob` 门控 `no_speech_prob`）；`transcribe.py` 经 `_seg_to_dict` 携带置信度字段。覆盖本次 6 条 sitcom 实战样本 + 单测。
- **当前核心瓶颈与新诉求**：
  1. 翻译风格单一体感，缺乏针对影视二创与学术科普的定制化分轨（建议 2）；
  2. 强 BGM、爆破、笑声掩盖下的音频，Whisper 偶尔存在底噪幻觉或微弱吞字，需前置伴奏分离（建议 3）；
  3. 声学对齐精度在极端语速下仍有毫秒级微小抖动，需引入强制对齐（原 T2）；
  4. 现有翻译引擎在 `--engine agent` 与基础机翻 `--engine google` 之间，缺少直接对接主流大模型 API（DeepSeek / OpenAI 兼容协议）的通道（建议 1 / 原 T5）；
  5. 缺乏轻量可视化的原片对齐与 `verify` 告警检查看板（建议 4 / 原 T4）。

### 0.2 铁律（不可违反）
1. **声学时间戳不可篡改**：转写与切分生成的时间戳为声学绝对事实，翻译和后处理阶段只改文本，绝不在下游重新算轴。
2. **跨平台兼容与优雅降级**：所有 GPU/Windows 专享特性（WhisperX、Demucs 人声分离、CUDA 加速）均为增量可选，在 Mac / CPU 环境下必须**自动平滑降级**或告警回退，绝不破坏基础流水线运行。
3. **分块断点续跑与缓存指纹防护**：任何影响转写产物的参数（模型、VAD、对齐后端、人声分离）必须纳入 chunk 缓存指纹（sha1），绝不误用脏缓存。
4. **SDD + TDD 先行**：每项新特性先定 Spec/ADR，测试覆盖（`pytest` 全绿 + 关键 golden 保护），文档随代码同步提交。

---

## 1. 任务路线图与优先级规划

```mermaid
flowchart TD
    T1[T1. CUDA 设备抽象与 .env 工具链隔离<br/>✅ 已完成] --> T2[T2. 智能人声/伴奏分离预处理<br/>✅ 已完成 ADR-017]
    T2 --> E1[E1. uv.lock 可复现安装<br/>🔒 下一阶段最高优先级]
    E1 --> E2[E2. ffmpeg 自动下载便携版<br/>🔒 消灭全盘搜]
    E2 --> E3[E3. 模型缓存校验与自愈]
    E3 --> E4[E4. CUDA 解析 venv torch/lib 优先]
    E4 --> T3[T3. 双轨翻译风格体系<br/>🎬 影视意译 / 📘 忠实直译]
    T3 --> T4[T4. WhisperX 强制声学对齐<br/>⏱️ 解决极端声学漂移]
    T4 --> T5[T5. 说话人分离 Diarization<br/>👥 pyannote 角色标签]
    T5 --> T6[T6. 独立大模型直连引擎<br/>🤖 DeepSeek / OpenAI API / Ollama]
    T6 --> T7[T7. 批量常驻服务 & Web 校对看板<br/>🖥️ FastAPI + Inspector UI]
```

---

## 2. 详细任务拆解

### T1 — CUDA 设备抽象与 .env 工具链隔离【✅ 已落地】
> **状态**：已在 `feat/v5-cuda-windows` 分支落地（ADR-014）。
- **主要产物**：
  - `src/video_translate/toolchain.py`：支持 `.env` / `.env.<platform>` 跨平台环境变量解析与自动注入。
  - `src/video_translate/config.py` & `cli.py`：支持 `VT_DEVICE`、`VT_COMPUTE_TYPE`（默认 `auto` 自动探测）。
  - 测试套件更新，全量回归测试通过。

---

### T2 — 智能人声/伴奏分离预处理（抗强 BGM 与底噪）【✅ 已落地】
> **状态**：已在 `feat/v5-cuda-windows` 分支落地（ADR-017 / Spec 19）。
- **主要产物**：
  - `src/video_translate/vocal_sep.py`：实现 `demucs` 伴奏剥离、16kHz mono 转换、指纹缓存与显存显式清理。
  - `src/video_translate/transcribe.py` & `fill_gaps.py`：在转写和补洞时挂接纯人声音轨（`vocals.wav`），时间戳严格锚定原视频真实时间轴。
  - `cli.py`：`transcribe` 与 `run` 命令新增 `--separate-vocals` 旗标。
  - `tests/test_vocal_sep.py`：13 个单元测试覆盖指纹生成、缓存重用、Demucs 不可用优雅降级等。

---

## 2A. E 系列 — 环境确定性工程（下一阶段最高优先级）

> **背景**（详见 [docs/RESEARCH-voice-pro.md](docs/RESEARCH-voice-pro.md)）：对标 Voice-Pro v4.0 的安装工程最佳实践，解决本项目的环境痛点——依赖版本飘移、ffmpeg「全盘搜」自由发挥、CUDA DLL 借外部项目路径、模型缓存残缺误判。目标是**新机器 clone 后 `make setup` 一次成功率逼近 100%**，Agent 无任何自由发挥空间。
>
> **共同约束**：每个任务落地时必须遵守 §3.2 依赖规则与 AGENTS.md §1 红线；先写测试（TDD）；文档随代码同步更新。

### E1 — uv.lock 可复现安装【P0】
> **问题**：`pyproject.toml` 已配 `[tool.uv.index]`（清华 cu124 镜像）与 `[tool.uv.sources]`（按平台选 wheel），但仓库**无 lockfile**——换机安装存在版本飘移风险，"依赖装错环境/装成 CPU 版"两类红线事故无法根治。
**动作清单**：
1. 在仓库根执行 `uv lock` 生成 `uv.lock` 并提交（确认 `.gitignore` 未排除它）。
2. `Makefile` 的 `setup` 目标改为 `uv sync` 优先（检测 `uv` 不存在时打印一条安装指引并回退 `pip install -e .`）。
3. `TOOLCHAIN.md` §3.1 与 `README.md` Quickstart 安装口径统一为：**uv sync 为标准路径，pip 为兜底**；`pyproject` 任何依赖变更后必须重跑 `uv lock` 并同 commit 提交。
**涉及文件**：`uv.lock`（新增）、`Makefile`、`TOOLCHAIN.md`、`README.md`
**验收标准**：
- 新 clone 目录下 `make setup` 一次成功（uv 路径），`uv lock --check` 通过；
- Windows/Linux 装出 `+cu124` torch，macOS 装出 CPU torch（复用既有 marker 验证）；
- 全量 `pytest` 绿。

### E2 — ffmpeg 自动下载便携版（消灭「全盘搜」）【P0】
> **问题**：`TOOLCHAIN.md` §2.1 第 3 步「全盘搜 C:/D:/E:/F: 找 ffmpeg.exe 写回 .env」是全流程中不确定性最高、最容易导致缓存散落的一步；且 `make setup` 不覆盖 ffmpeg。
**动作清单**：
1. `toolchain.py` 新增 `ensure_ffmpeg(dest="tools/ffmpeg") -> str | None`：按 `sys.platform` **先选平台再选源**下载便携版并解压——Windows：镜像 → [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) release-full (zip)；Linux：静态构建 (tar.xz)；macOS：evermeet/镜像 (zip)。下载走既有 `proxy.detect_proxy` 机制；**禁止跨平台复用同一包**。
2. `cli.py` `setup` 子命令新增 `--ffmpeg` 旗标：检测 ffmpeg 缺失时下载、解压至 `tools/ffmpeg/`，并自动把 `VT_FFMPEG_DIR` 写入 `.env.local`（gitignore 内，机器私有）。
3. `doctor` 在 ffmpeg `[MISS]` 时输出提示行：`run: video-translate setup --ffmpeg`。
4. `TOOLCHAIN.md` §2.1 三步探测**收敛为两步**：① 系统 PATH → ② `setup --ffmpeg` 自动下载；「全盘搜」从文档删除，降级为人工兜底不再写入协议。
5. `tools/` 目录加入 `.gitignore`（二进制不进 git，与 `models/` 同理）。
**涉及文件**：`src/video_translate/toolchain.py`、`src/video_translate/cli.py`、`tests/test_toolchain.py`（mock 下载，不真联网）、`TOOLCHAIN.md`、`.gitignore`
**验收标准**：
- 在无 ffmpeg PATH 的环境跑 `video-translate setup --ffmpeg` 后 `doctor` ffmpeg/ffprobe `[OK]`；
- 单测覆盖：下载 mock、zip 解压、`.env.local` 写入、已存在时幂等跳过；
- 全量 `pytest` 绿。

### E3 — 模型缓存完整性校验 + 自愈【P1】
> **问题**：`_model_cached`（cli.py:59）只检查 `model.bin` 存在性——下载中断的残缺文件会被误判已缓存，随后 `run` 在转写深处加载崩溃，报错对新手不友好。
**动作清单**：
1. `_model_cached` 增加大小下限校验（`model.bin` < 2GB 视为残缺；large-v3 完整约 3.09GB）。
2. `cmd_setup` 发现残缺缓存时删除该 snapshot 目录并重新下载（自愈）。
3. `transcribe_video` 的 `WhisperModel(...)` 构造包一层异常捕获，失败时打印明确指引：`模型加载失败（可能缓存损坏）。修复：video-translate setup`，然后以 `EXIT_MISSING_DEP` 退出，不再裸 traceback。
**涉及文件**：`src/video_translate/cli.py`、`src/video_translate/transcribe.py`、`tests/`（新增残缺缓存用例：构造小尺寸假 model.bin 验证检测/自愈路径）
**验收标准**：
- 残缺缓存场景：`setup` 能检出并重下（单测 mock 下载）；`run` 阶段报错信息含修复命令；
- 正常缓存不受影响（幂等）。

### E4 — CUDA 解析顺序：venv torch/lib 优先【P1】
> **问题**：CUDA DLL 当前依赖 `.env.win` 硬编码 `VT_CUDA_DIR=F:\win-pyvideotrans-v3.92\_internal\torch\lib`（借外部项目的包）。新机器若没装过 pyvideotrans 则 GPU 不可用。Voice-Pro 已验证：PyTorch cu1xx wheel 自带完整 CUDA 运行时（cublas/cudnn），本项目 venv 内 `site-packages/torch/lib` 就有同一套 DLL。
**动作清单**：
1. `toolchain.py` `init_toolchain` 的 CUDA 目录解析顺序改为：**① venv 内 `torch/lib`（自动探测，`import torch; torch.__file__` 定位）→ ② `VT_CUDA_DIR` 显式覆盖（仍最高优先级若显式设置）→ ③ 无则 CPU 降级**。注意：显式 `VT_CUDA_DIR` 应优先于自动探测（用户明确指定时不抢夺）。
2. `doctor` 输出 CUDA 来源标注（`venv-torch` / `env` / `none`）。
3. `.env.win.example` 移除 pyvideotrans 硬编码示例，注明「通常无需配置；仅当 venv torch/lib 缺 DLL 时手工指定」。
**涉及文件**：`src/video_translate/toolchain.py`、`tests/test_toolchain.py`（解析顺序单测：显式 env 优先、自动探测、无 torch 回退）、`.env.win.example`、`TOOLCHAIN.md` §2.2
**验收标准**：
- 有 GPU + venv cu124 torch 的机器**不配** `VT_CUDA_DIR` 也能 `device=cuda`；
- 显式 `VT_CUDA_DIR` 仍优先生效；无 torch/torch 无 DLL 时静默 CPU 降级不崩溃；
- `doctor` 正确标注来源；全量 `pytest` 绿。

---

### T3 — 双轨翻译风格体系（影视意译 vs 忠实直译）【里程碑 4 / E 系列完成后启动】
> **背景**：不同视频场景对翻译诉求完全不同——电影/美剧/脱口秀需要“口语化、接地气、短促有力、情绪饱满”；而科技演讲/公开课/财报会议则需要“术语严谨、概念忠实、保留逻辑从句”。
**核心设计：**
1. **预设 Persona 矩阵**：
   - `film`（默认/影视二创）：信达雅 + 口语感，短句节奏优先，文化梗意译，限制单行字数。
   - `literal`（忠实直译）：严谨对齐原文主谓宾，保留学术/专业修饰，专有名词严格忠实。
   - `bilingual_study`（双语精读）：直译为主，生僻词/熟词生义在括号内追加注记。
2. **CLI 与配置接入**：
   - `--style {film,literal,bilingual_study}`（或 `VT_STYLE`），注入 `translate_task.json` 的 `persona` 与 `guidelines`。
3. **输出多轨可选**：
   - 支持通过参数同时生成两套独立字幕（如 `<base>.film.bilingual.srt` 与 `<base>.literal.bilingual.srt`），方便创作者对比选优。

---

### T4 — WhisperX 强制声学对齐（修极端声学漂移）【GPU 专享】
> **背景**：ADR-013 决策。在 Windows/Linux GPU 环境下，通过 wav2vec2 模型进行词级强制对齐，将词时间戳精度从 82% 提升至 96% 以上。
**核心设计：**
1. CLI 增加 `--align {none,whisperx}`（默认 `none`）。
2. 仅在 Windows/Linux 且安装了 `whisperx` 时调用；在 Mac/无该库环境下显式告警并**优雅降级回退 `none`**，绝不崩溃。
3. 对齐只优化词级时间戳，绝不修改文本内容与断句分组。
4. 缓存指纹中追加 `align` 维度，隔离不同对齐模式的缓存。

---

### T5 — 说话人分离（Diarization，随 WhisperX 扩展）
**核心设计：**
1. 基于 pyannote 管道，识别多人交谈中的发言人身份。
2. CLI 增加 `--diarize` 开关（需配置 `HF_TOKEN`）。
3. 在 `merge.py` 与 `generate.py` 中为不同角色的台词添加可配置的 Speaker 标识（如 `[Speaker 1]: ...`），并映射到 `translate_task.json` 中辅助大模型识别说话人关系。

---

### T6 — 独立大模型直连引擎（`--engine llm`：DeepSeek / OpenAI / Ollama）
> **背景**：解决无 Agent 介入时、纯脚本/流水线批处理场景下 Google 机器翻译质量不足的问题。
**核心设计：**
1. **统一 LLM Client 抽象**：
   - 支持任何兼容 OpenAI 接口标准的提供商（如 DeepSeek、OpenAI、Moonshot、Ollama 本地 7B/14B 等）。
2. **环境配置与参数**：
   - `.env` 支持：`VT_LLM_API_KEY`、`VT_LLM_BASE_URL`（如 `https://api.deepseek.com/v1`）、`VT_LLM_MODEL`（如 `deepseek-chat`）。
   - CLI 扩展 `--engine {agent,google,llm,ollama}`。
3. **批量并发与鲁棒重试**：
   - 结构化读取 `translate_task.json` 的 batch 列表，通过标准 Prompt 调用，自动解析 JSON 返回并组装成 `zh_segments.json`。
   - 自动内置指数退避与 JSON 格式校验修复机制。

---

### T7 — 批量常驻服务与 Web UI 校对看板（Inspector）
> **背景**：提供简单直观的图形化交互界面，降低操作门槛，并实现 `verify` 门禁结果的可视化精修。
**核心设计：**
1. **后台 FastAPI 服务**：
   - 提供视频上传、任务提交、进度轮询（`GET /jobs/{id}`）与产物下载接口。
2. **WebUI 校对看板 (Subtitle Inspector)**：
   - 左侧：原视频/音频同步播放器（支持点击字幕跳转到对应时间点）。
   - 右侧：双语字幕列表，**高亮标出 `verify` 触发的异常行**（如声学跨静音、漏译英文夹生词、低置信度段落）。
   - 交互：支持直接在线双击微调中文字幕文本，一键重新打包生成 4 个最终产物文件。

---

## 3. 依赖与环境隔离矩阵

| 特性模块 | 依赖项 | 依赖分组 (pyproject.toml) | 运行环境要求 |
|---|---|---|---|
| **基础转写 & Agent 翻译** | `faster-whisper`, `deep-translator` | 核心依赖 (无附加) | 跨平台 (Win / Mac / Linux) |
| **.env 工具链隔离** | 纯 Python 标准库 (os/re/sys) | 核心依赖 | 跨平台 |
| **智能人声分离 (T2)** | `demucs`, `torch`, `torchaudio` | 核心依赖 (默认安装) | 跨平台：Windows/Linux 自动 CUDA wheel，macOS 自动 CPU wheel |
| **环境确定性 (E1-E4)** | `uv`（外部工具，不进 pyproject）；ffmpeg 便携包（运行时下载，不进 git） | 无新增 Python 依赖 | 跨平台 |
| **双轨翻译风格 (T3)** | 纯 Prompt 与业务逻辑 | 核心依赖 (无附加) | 跨平台 |
| **强制对齐 & 说话人 (T4/T5)** | `whisperx`, `pyannote.audio` | `[windows]` / `[gpu]` extra | 需 NVIDIA CUDA 12 + Python 3.12 |
| **独立 LLM API (T6)** | `httpx` (支持异步高并发) | `[llm]` extra | 跨平台 |
| **Web UI 看板 (T7)** | `fastapi`, `uvicorn`, 轻量静态前端 | `[web]` extra | 跨平台 |

### 3.2 依赖与外部工具管理规则（R1-R7，长期固化）

> 本规则自 2026-08-25 起生效，约束**此后所有新增工具、依赖与二进制资产**。来源：既有红线（AGENTS.md §1 / TOOLCHAIN.md §3.1）+ Voice-Pro 对标研究。执行模型在动任何依赖前必须逐条对照。

| # | 规则 | 反例（禁止） | 正例 |
|---|---|---|---|
| R1 | **运行时 Python 依赖一律写进 `pyproject` 顶层 `dependencies`**；dev-only 工具（pytest/lint）进 `[project.optional-dependencies].dev` | 把 `demucs` 藏进 `[audio]` extra 导致默认安装缺失 | demucs/torchaudio 均在顶层 |
| R2 | **CUDA wheel 只走镜像索引，绝不裸装**：`uv sync` 认 `[tool.uv.sources]`；pip 必须显式 `--index-url` 清华 cu124 | 裸 `pip install torch` 装成 `+cpu` | `uv sync` / `pip install --index-url …/pytorch-wheels/cu124/` |
| R3 | **`uv.lock` 是依赖唯一事实来源**：任何 `pyproject` 依赖变更，必须在同一 commit 内重跑 `uv lock` 提交（E1 落地后生效） | 改了 pyproject 不更新 lockfile，换机版本飘移 | pyproject + uv.lock 成对变更 |
| R4 | **外部二进制（ffmpeg 等）不手动安装、不进 git**：统一由 `setup --ffmpeg` 自动下载到 `tools/`（gitignore），路径写 `.env.local` 登记 | Agent 全盘搜 ffmpeg.exe 写回协议；把 ffmpeg.exe 提交进仓库 | `video-translate setup --ffmpeg` 一步到位 |
| R5 | **模型权重不进 git，跨项目共享 HF cache**：默认 `~/.cache/huggingface`（`HF_HOME` 可覆盖）；`models/<name>/` 仅作离线 drop-in 可选项；缓存必须过完整性校验（E3） | 每项目塞一份 3GB 权重；残缺 model.bin 静默使用 | `make setup` 拉共享缓存 + 自愈校验 |
| R6 | **新依赖准入检查清单**（合并前逐项确认）：① 跨平台？（Mac/CPU 降级路径）② 影响转写产物→是否需进 chunk 缓存指纹？③ GPU 显存预算（8GB 红线，单一大模型串行）④ 是否有纯标准库/既有依赖的等价实现？⑤ lockfile 同步 | 引入 pyannote 但不检查 Mac 降级，Mac 用户 pip 装不上 | 每项检查写入对应任务 Spec |
| R7 | **镜像/代理固化在配置，不靠临场决策**：PyPI/PyTorch 镜像进 `[tool.uv.index]` / `PIP_EXTRA_INDEX_URL`；HF 镜像进 `HF_ENDPOINT`；安装时任何 Agent/人工不得现场拼源 | 每次安装现场挑镜像，换机不可复现 | TOOLCHAIN.md §1.2 环境变量段 |

**新依赖 PR 模板核对项**：`pyproject 顶层 ✓ / lockfile 同步 ✓ / 准入清单 R6 五项 ✓ / 文档（TOOLCHAIN §矩阵）同步 ✓`

---

## 4. 推荐实施顺序与迭代里程碑

1. **里程碑 1 (已完成)**：T1 CUDA 抽象与 `.env` 工具链隔离，文档全面梳理完毕。
2. **里程碑 2 (已完成)**：T2 智能人声/伴奏分离预处理（ADR-017 / Spec 19 落地，Demucs 纯人声剥离 + 显存显式清理 + 缓存指纹）。
3. **里程碑 3 (下一阶段核心，2026-08-25 重定) — 环境确定性工程（E1-E4）**：
   - E1 uv.lock 可复现安装 → E2 ffmpeg 自动下载 → E3 模型缓存自愈 → E4 CUDA venv 优先，**按序执行**（E1 是其余任务的地基）。
   - 完成判据：新 clone 机器 `make setup` + `setup --ffmpeg` 两条命令后 `doctor` 全绿，无任何手动 PATH/全盘搜/改 .env 操作。
4. **里程碑 4 (业务功能恢复)**：
   - **第一步**：实施 **T3 双轨翻译风格体系**（纯逻辑与 Prompt 体系，扩展影视口语二创与严谨直译两套译文）。
   - **第二步**：实施 **T4 WhisperX 强制对齐** 与 **T5 说话人分离**（GPU 盒专享加速）。
5. **里程碑 5 (自动化与可视化闭环)**：
   - 实施 **T6 独立大模型直连引擎**（DeepSeek / 本地 Ollama 自动化）。
   - 实施 **T7 Web UI 批量服务与可视化校对看板**。

---

## 5. 打包与 Windows 部署

- **工具**：PyInstaller onefile，`Makefile` 加目标
  ```make
  build-win:
  	py -3.12 -m venv .venv-win && .venv-win\Scripts\activate && \
  	pip install -e ".[windows]" && \
  	pyinstaller --onefile --name video-translate src/video_translate/cli.py
  ```
- **CUDA DLL**：把 Purfview `whisper-standalone-win` 的 NVIDIA 库（cuBLAS/cuDNN 12）复制到 exe 同目录并加入 PATH；或在文档里要求装 CUDA Toolkit 12.x。
- **首次运行**：需联网拉 `large-v3` 进 Windows HF 缓存（`%USERPROFILE%\AppData\Local\huggingface`），之后离线。
- **专项测试**：中文路径、文件锁、长视频续跑（`transcribe_video` 已有 chunk 续跑，需确认 Windows 下同样生效）。

---

## 6. 风险与回退

| 风险 | 触发条件 | 对策 |
|---|---|---|
| faster-whisper 升级改 VAD | 引入 WhisperX 自带更新版 | Mac 锁 `1.2.1`；WhisperX 仅 Windows extra；升级后回测 B2 类误杀 |
| 8GB OOM | `float16` + 大模型同驻 | 转写用 `int8_float16`；转写/翻译/对齐**分步**执行；必要时降模型 |
| WhisperX 与 1.2.1 冲突 | 依赖不兼容 | 回退 `stable-ts`（3.12 可装）或仅对齐不换核心 |
| ctranslate2 无 3.13 wheel | Windows 误用 3.13 | 构建 venv 强制 3.12 |
| 中文路径 / 文件锁 | Windows 文件系统差异 | 部署前专项测试 |
| cuDNN 9 冲突 | `nvidia-cudnn-cu12` 9.x 异常 | 对齐 cuDNN 版本；必要时钉 `ctranslate2` 版本 |

---

## 7. 验收标准（每任务）

- **T1**：Mac 本地回归通过（`device=auto` 等价原 `cpu/int8`，产物与历史一致）；Windows `nvidia-smi` 下 `device=cuda` 生效、速度提升；cpu/int8 产物与历史一致。【已通过】
- **T2**：`--separate-vocals` 成功分离出 `vocals.wav` 喂给 Whisper，强 BGM 场景无多余幻觉，时间戳保持 100% 原始对齐。【已通过，13 条单测全绿】
- **E1**：新 clone 环境 `make setup`（uv 路径）一次成功；`uv lock --check` 通过；平台 marker 验证（Win/Linux → cu124，macOS → cpu）。
- **E2**：无 ffmpeg PATH 的环境 `video-translate setup --ffmpeg` 后 `doctor` 全绿；下载/解压/登记全流程单测（mock 网络）覆盖。
- **E3**：残缺缓存（<2GB 假 model.bin）被检出并自愈重下；`run` 阶段模型加载失败输出含修复命令的指引。
- **E4**：不配 `VT_CUDA_DIR` 时 GPU 机器自动用 venv torch/lib 命中 CUDA；显式 `VT_CUDA_DIR` 仍优先；无 GPU 静默降级 CPU；`doctor` 标注 CUDA 来源。
- **T3**：`--style film` 与 `--style literal` 能产出对应风格的译文，支持双轨输出。
- **T4**：鲍德温类漂移样本时间戳误差 < 150ms；可用 `verify --video` 声学 lane 量化（ADR-012 / Spec 18）。
- **T5**：多人视频 cue 带 `Speaker N:` 标签。
- **T6**：`--engine llm` 支持直接调用 DeepSeek / OpenAI API 自动完成翻译与格式自愈。
- **T7**：Web 提交 → 产出全流程跑通，Inspector 看板高亮 `verify` 异常行并支持在线微调。

---

## 8. 分支与文档协同

- GitHub 处于 `feat/v5-cuda-windows` 分支进行开发。
- 已落地 ADR 引用：
  - **ADR-011**：VAD 由默认开改为选开（默认关 / 裸跑）。
  - **ADR-012**：修订「时间戳是声学事实」不变量，引入独立声学参照 + `verify` 三 lane。
  - **ADR-013**：WhisperX 强制对齐（GPU 盒）引入决策（对应 T4）。
  - **ADR-014**：撤销 ADR-001 的 CUDA 硬编码禁令，`device`/`compute_type` 改为 `auto` 自动探测（对应 T1）。
  - **ADR-020**：尾部回音幻觉防御——第四信号（共享音频确定性指纹）+ 第五信号（Whisper 置信度字段），补 V4 双信号盲区（对应 sitcom 实战发现的 57s 回音）。
- **研究输入**：[docs/RESEARCH-voice-pro.md](docs/RESEARCH-voice-pro.md)（2026-08-25，E 系列与 §3.2 规则的论证来源）。
- **依赖规则**：§3.2 R1-R7 与 [TOOLCHAIN.md](TOOLCHAIN.md) §3.1、[AGENTS.md](AGENTS.md) §1 红线表三处互为引用，修订时须三处同步。
- 本计划文档（`MAJOR_VERSION_PLAN.md`）随仓库走，作为后续任务开发的唯一事实来源。
