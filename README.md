# video-translate

> 🎬 **视频转剪映中英双语字幕工具**：基于 faster-whisper 的声学高保真转写、Agent 即引擎（Agent-as-engine）高质量上下文翻译、剪映即插即用双语字幕输出与三维质量自检门禁。

---

## 🌟 核心特性 (Key Highlights)

- **🎙️ 声学绝对对齐 (Acoustic-Accurate Alignment)**：严格保留 whisper 转写产生的底层时间戳，下游断句与翻译**只改文本、绝不重算时间轴**，彻底杜绝字幕音画漂移（[ADR-012](docs/adr/012-acoustic-timestamp-truth.md)）。
- **🤖 Agent 即引擎 (Agent-as-Engine)**：CLI 专注于声学重计算与切分，将翻译任务以结构化 JSON 抛给宿主 AI Agent（Claude / Cursor / VS Code Copilot 等）完成高质量上下文翻译，本地无需配置庞大 LLM 运行时；同时提供 `--engine google` 作为全自动无头兜底（[ADR-005](docs/adr/005-agent-as-engine.md)）。
- **⚡ 硬件自适应与工具链隔离**：支持 NVIDIA CUDA 自动加速与 CPU/int8 平滑降级；通过 `.env` / `.env.<platform>` 自动加载 FFmpeg 与 CUDA 库，彻底解耦宿主环境与业务代码（[TOOLCHAIN.md](TOOLCHAIN.md)）。
- **🛡️ 三维质量护栏 (Three-Lane Guardrails)**：
  - **声学层**：基于 `silencedetect` 独立参考自检跨静音与漏检（`uncovered-audio`），动态音频画像自动推荐 VAD 模式。
  - **内容层**：翻译行数覆盖率检查 + Pearson 索引对齐防串行 + 未翻译英文检测 + 语义回读任务。
  - **表现层**：默认维持 `tail 0.3s / min-dur 1.0s` 呼吸余量；输出目录自动递增版本号（`_v2`, `_v3`），规避剪映导入同名文件的内部缓存失效问题。
- **🔄 断点续跑与分块恢复 (Resumable Pipeline)**：转写过程按分块持久化缓存（`chunk_N.json`），中断后重跑自动跳过已完成分块，长视频重试零浪费。

---

## 🏗️ 架构与工作流 (Architecture & Workflow)

```mermaid
flowchart TD
    Video[输入视频/音频] --> Doctor[0. doctor 环境自检 & 音频画像推荐 VAD]
    Doctor --> Run[1. video-translate run <video>]
    
    subgraph Acoustic [声学阶段 (本地 CLI)]
        Run --> Transcribe[faster-whisper 转写 (分块可续跑 chunk_N.json)]
        Transcribe --> Merge[断句合并 / 幻觉过滤 / 漂移吸附]
        Merge --> FillGaps[fill_gaps 漏音补洞自检]
        FillGaps --> TaskOut[输出 translate_task.json]
    end

    TaskOut --> Exit6[CLI 退出码 6: EXIT_AWAITING_AGENT 挂起]

    subgraph AgentBrain [内容翻译阶段 (Agent / 人)]
        Exit6 --> AgentRead[Agent 阅读全局剧本 + 上下文 + 角色设定]
        AgentRead --> AgentTranslate[Agent 翻译生成 zh_segments.json]
    end

    subgraph Presentation [表现 & 交付阶段 (本地 CLI)]
        AgentTranslate --> Generate[2. video-translate generate]
        Generate --> AlignCheck[自动校验 zh/en 索引对齐 Pearson 相关性]
        Generate --> Render[输出带 offset/tail 保护的双语 SRT / TXT]
        Render --> VersionDir[落地为防剪映缓存失效的子目录 <base>_vN/]
        VersionDir --> Verify[3. video-translate verify 门禁校验]
    end
```

---

## ⚡ 极速上手 (Quick Start)

### 1. 安装项目与依赖
```bash
# 激活 Python 虚拟环境 (Python >= 3.10)
python -m venv .venv

# Windows 激活:
.venv\Scripts\Activate.ps1
# macOS/Linux 激活:
source .venv/bin/activate

# 安装依赖与当前包（可编辑模式）
pip install -e .
```

### 2. 配置本地工具链（FFmpeg / CUDA）
根据操作系统复制对应的环境模板（详细说明见 [TOOLCHAIN.md](TOOLCHAIN.md)）：
```bash
# Windows
copy .env.win.example .env.win
# macOS
cp .env.mac.example .env.mac
# Linux
cp .env.linux.example .env.linux
```
在 `.env.win` 中配置你本地的工具路径（若系统 PATH 中已有则无需填写）：
```dotenv
VT_FFMPEG_DIR=F:\win-pyvideotrans-v3.92\ffmpeg
VT_CUDA_DIR=F:\win-pyvideotrans-v3.92\_internal\torch\lib
```

### 3. 环境自检 (Doctor)
```bash
video-translate doctor
```
确保 `ffmpeg`、`ffprobe` 和模型缓存处于 `[OK]` 状态。

### 4. 运行完整管线

#### 模式 A：Agent 引擎模式（推荐，默认）
```bash
# 1. 运行转写并生成翻译任务
video-translate run "videos/example.mp4"
# 程序转写完成后会返回 Exit Code 6 挂起，并输出 videos/example.translate_task.json

# 2. AI Agent（或人工）阅读 task 文件后，生成 videos/example.zh_segments.json

# 3. 生成双语字幕与文本文件
video-translate generate --segments "videos/example.segments_en.json" --zh "videos/example.zh_segments.json" --outdir "videos/example" --base "example"

# 4. 运行质量自检门禁
video-translate verify --segments "videos/example.segments_en.json" --zh "videos/example.zh_segments.json" --video "videos/example.mp4"
```

#### 模式 B：Google 翻译无头模式（全自动）
```bash
video-translate run "videos/example.mp4" --engine google
```

---

## 🛠️ CLI 命令与参数速查 (CLI Reference)

| 命令 (Subcommand) | 作用 | 核心参数示例 |
|---|---|---|
| `doctor` | 检查环境依赖、GPU 状态，分析视频音频画像推荐 VAD | `video-translate doctor --video "videos/sample.mp4"` |
| `run` | 一站式执行流水线（转写 $\rightarrow$ 任务生成 $\rightarrow$ 生成字幕） | `video-translate run "videos/sample.mp4" [--vad] [--adaptive-vad]` |
| `transcribe` | 仅执行音频抽取、Whisper 转写、合并断句与漏音补洞 | `video-translate transcribe "videos/sample.mp4"` |
| `translate` | 执行翻译任务（Agent 模式下生成 task，Google 模式下直接调用） | `video-translate translate --segments "...segments_en.json" --out "...zh_segments.json"` |
| `generate` | 将中英文合并生成 4 个产物，自动防剪映同名缓存碰撞 | `video-translate generate --segments "...segments_en.json" --zh "...zh_segments.json"` |
| `verify` | 运行声学、内容、表现三维度门禁校验与语义回读 | `video-translate verify --segments "...segments_en.json" --zh "...zh_segments.json" --video "...mp4"` |
| `backfill` | 针对 Google 模式下失败的段落进行回填补录 | `video-translate backfill --pending "...agent_pending.json" --out "...zh_segments.json"` |
| `resegment` | 对特定时间窗口强制重转写指定语言（如修复混合语种） | `video-translate resegment --segments "...segments_en.json" --video "...mp4" --windows 12.0-18.5 --lang ja` |
| `setup` | 检查并按需下载 faster-whisper `large-v3` 模型 | `video-translate setup [--model large-v3]` |

---

## ⚙️ 配置分层与优先级

参数解析优先级从高到低为：
```
CLI 参数 > 系统环境变量 / .env.local > .env.<platform> > .env > .video-translate.toml > 默认配置
```

### 常用环境变量表
| 环境变量 | 对应配置 | 默认值 | 作用说明 |
|---|---|---|---|
| `VT_FFMPEG_DIR` | - | `None` | FFmpeg/FFprobe 可执行文件目录（自动注入 PATH） |
| `VT_CUDA_DIR` | - | `None` | CUDA 运行库目录（Windows 自动注入 PATH 并添加 DLL 目录） |
| `VT_MODEL` | `model` | `large-v3` | 默认 Whisper 模型名称或本地目录路径 |
| `VT_DEVICE` | `device` | `auto` | 计算设备：`auto` (优先 CUDA，无 GPU 退回 cpu) / `cuda` / `cpu` |
| `VT_COMPUTE_TYPE`| `compute_type` | `auto` | 量化精度：`auto` (CUDA 为 `int8_float16`，CPU 为 `int8`) |
| `VT_CHUNK` | `chunk` | `240.0` | 转写分块时长（秒），支持断点续跑 |
| `VT_ENGINE` | `engine` | `agent` | 翻译引擎：`agent` (任务分发) 或 `google` (无头模式) |
| `VT_PROXY` | `proxy` | `None` | HTTP 代理地址（仅 Google 引擎与模型下载需用，SOCKS 不支持） |
| `HF_ENDPOINT` | - | `None` | 国内 HuggingFace 镜像源（如 `https://hf-mirror.com`） |
| `PIP_EXTRA_INDEX_URL` | - | `None` | 国内 PyTorch wheel 镜像（CN 无代理安装用，如 `https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cu124/`；`uv sync` 已内置，pip 需手动设） |

---

## 📂 项目产物说明

执行完成后，在视频对应子目录下会生成以下 4 个标准交付文件：
- `<base>.bilingual.srt`：**中英双语字幕**（剪映直接导入主文件，顶部英文/底部中文）
- `<base>.zh.srt`：纯中文字幕
- `<base>.en.srt`：纯英文字幕
- `<base>.txt`：中英文双语对照纯文本剧本

---

## 📖 文档导航中心 (Documentation Index)

- 🤖 **[AGENTS.md](AGENTS.md)**：AI Agent 执行协议、避坑防呆红线速查与确定性状态机。
- 🛠️ **[TOOLCHAIN.md](TOOLCHAIN.md)**：工具链引导、CUDA 配置、模型离线下载与环境隔离。
- 📜 **[docs/HISTORY.md](docs/HISTORY.md)**：完整的版本演进史、实战案例与踩坑复盘（V3–V14）。
- 📐 **[docs/specs/](docs/specs/) & [docs/adr/](docs/adr/)**：系统设计规格 (SDD) 与架构决策记录 (ADR)。

---

## 📄 开源许可证 (License)

本项目基于 **MIT 许可证** 发布，完整文本见 [LICENSE](LICENSE)。

Copyright (c) 2026 BruceYang
