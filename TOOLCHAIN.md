# 工具链与环境隔离指南 (TOOLCHAIN)

> 本文件是本项目关于**外部工具链（FFmpeg/FFprobe）、GPU/CUDA 运行时、Whisper 模型资产及环境配置（.env）**的唯一权威指引。
> 项目代码已支持**自动探测与环境注入**，通过 `.env` 系列配置文件实现跨平台与宿主环境解耦。

---

## 1. 核心架构与环境配置机制

为了实现跨系统（Windows / macOS / Linux）通用性并避免 Agent / 人工每次手动输入环境变量，本项目采用分层的 `.env` 自动加载机制。

### 1.1 配置文件加载层级（优先级从高到低）
```
CLI 参数 / 系统运行时 os.environ  >  .env.local (本地私有)  >  .env.<platform> (.env.win / .env.mac / .env.linux)  >  .env (通用基础)  >  .video-translate.toml  >  默认值
```

| 配置文件 | 作用 | 是否提交 Git |
|---|---|---|
| `.env.example` | 通用环境变量模板 | 是 |
| `.env.win.example` | Windows 平台配置模板 | 是 |
| `.env.mac.example` | macOS 平台配置模板 | 是 |
| `.env.linux.example` | Linux 平台配置模板 | 是 |
| `.env` / `.env.win` / `.env.mac` / `.env.linux` | 本机生效的环境配置 | 否（已 gitignore） |
| `.env.local` | 针对单机最高优先级的临时覆盖 | 否（已 gitignore） |

### 1.2 常用环境变量说明
- `VT_FFMPEG_DIR`：FFmpeg / FFprobe 所在目录（包含 `ffmpeg.exe` / `ffmpeg`）。程序启动时会自动加入 `PATH`。
- `VT_CUDA_DIR`：CUDA / cuBLAS / cuDNN 运行时 DLL 目录（Windows 下程序会自动加入 `PATH` 并调用 `os.add_dll_directory`）。
- `VT_MODEL`：默认 Whisper 模型名称或本地模型目录路径（默认 `large-v3`）。
- `HF_HOME`：HuggingFace 模型缓存目录（默认 `~/.cache/huggingface`）。
- `HF_ENDPOINT`：HuggingFace 镜像源（如 `https://hf-mirror.com`）。
- `PIP_EXTRA_INDEX_URL`：PyTorch wheel 国内镜像（CN 无代理安装用）。CUDA 12.4 例：
  `https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cu124/`。
  **已固化进 `pyproject.toml` 的 `[tool.uv.index]`**（Windows/Linux 自动走清华镜像），
  用 `uv sync` 时无需手动设；用 `pip` 时才需设此环境变量兜底。
- `VT_DEVICE`：计算设备（`auto` / `cuda` / `cpu`）。
- `VT_COMPUTE_TYPE`：量化类型（`auto` / `int8_float16` / `int8` / `float16`）。
- `VT_ENGINE`：翻译引擎（默认 `agent`，可选 `google`）。
- `VT_PROXY`：HTTP 代理地址（如 `http://127.0.0.1:7890`，仅在 `--engine google` 时使用）。

---

## 2. 外部依赖工具链详情

外部工具链由以下三大组件构成：

### 2.1 FFmpeg / FFprobe（音视频处理核心）
- **作用**：音频抽流（16kHz 单声道 WAV）、音量与静音画像（`volumedetect`、`silencedetect`）、时长探测。
- **探测与处理顺序**（先 Pass、再读配置、后全盘搜、真缺失才提示）：
  1. **(1) 探测系统环境**：若 `ffmpeg -version` 可直接运行，则直接使用。
  2. **(2) 自动读取 .env 配置**：若未在系统 PATH，读取 `.env` / `.env.win` 中的 `VT_FFMPEG_DIR`，程序自动注入运行时 PATH。
  3. **(3) 本地搜寻登记**：若上述路径不存在，Agent 或用户可在常见盘（C:/D:/E:/F:）全盘搜索 `ffmpeg.exe`，找到后写入 `.env.win`（或相应平台文件），以后长期生效。
  4. **(4) 确认缺失后提示**：若全盘皆无，方判定为缺失，提示用户安装（`winget install Gyan.FFmpeg` 或 `brew install ffmpeg` 或手动下载解压）。

### 2.2 CUDA 运行时库（GPU 推理加速）
- **作用**：faster-whisper 基于 CTranslate2 后端，在 NVIDIA GPU 下可实现 5-10x 实时加速。
- **动态库依赖**：需要 `cublas64_12.dll`、`cublasLt64_12.dll`、`cudart64_12.dll`、`cudnn64_9.dll` 等。
- **配置方式**：
  - 若系统安装了标准 CUDA Toolkit，会自动识别。
  - 若使用绿色版/打包的 CUDA 运行库（如 Python torch\lib 目录），在 `.env.win` 中指定 `VT_CUDA_DIR`（例如 `VT_CUDA_DIR=F:\win-pyvideotrans-v3.92\_internal\torch\lib`）。
- **CPU 自动回退机制**：
  - 当 `VT_DEVICE=auto` 时，系统先探测 CUDA。若无 GPU 或 CUDA 依赖库缺失/加载失败，系统会自动降级为 `--device cpu --compute-type int8` 运行，不会直接崩溃中断。

### 2.3 Whisper 模型（large-v3，约 3GB）
- **本地优先原则**：
  - 程序启动时，优先检测项目根目录 `models/large-v3/`（需含 `model.bin`）。若存在，直接离线加载，完全不依赖网络。
  - 其次检测系统共享缓存 `~/.cache/huggingface/hub/`。
- **网络镜像与离线下载**：
  - 如需在线拉取，在 `.env` 中设置 `HF_ENDPOINT=https://hf-mirror.com`，然后执行 `video-translate setup`。
  - 无网络环境下，用户可从镜像源下载完整模型包并解压至项目根 `models/large-v3/`。

---

## 3. 本机快速上手与验证

### 3.1 首次初始化步骤
1. 根据你的操作系统，复制对应的模板：
   ```bash
   # Windows:
   copy .env.win.example .env.win
   # macOS:
   cp .env.mac.example .env.mac
   # Linux:
   cp .env.linux.example .env.linux
   ```
2. 在 `.env.win`（或对应文件）中填入你机器上的实际路径：
   ```dotenv
   VT_FFMPEG_DIR=F:\win-pyvideotrans-v3.92\ffmpeg
   VT_CUDA_DIR=F:\win-pyvideotrans-v3.92\_internal\torch\lib
   ```
3. 运行环境自检：
   ```bash
   video-translate doctor
   ```
   若输出中 `ffmpeg`、`ffprobe`、`large-v3 model` 均为 `[OK]`，且 `device` 处于预期状态，说明工具链初始化成功。

---

## 4. 常用执行命令

```bash
# 1. 预检
video-translate doctor

# 2. 跑管线（默认 agent 引擎，转写完成后输出 translate_task.json 并返回 exit code 6）
video-translate run "videos/example.mp4"

# 3. 生成最终字幕文件
video-translate generate --segments videos/example.segments_en.json --zh videos/example.zh_segments.json --outdir videos/example --base example

# 4. 门禁验证
video-translate verify --segments videos/example.segments_en.json --zh videos/example.zh_segments.json --video videos/example.mp4
```

---

## 5. 常见问题排查 (Troubleshooting)

1. **报 `ffmpeg/ffprobe not found in PATH`**：
   - 检查 `.env` 或 `.env.win` 中的 `VT_FFMPEG_DIR` 是否指向包含 `ffmpeg.exe` 的文件夹。
   - 运行 `video-translate doctor` 查看 `env config` 行是否成功加载了配置文件。
2. **报 `Could not load library cublas64_12.dll`**：
   - 说明 GPU 模式被激活，但缺少 CUDA 12 动态库。
   - 在 `.env.win` 中补充 `VT_CUDA_DIR` 指向包含该 dll 的目录，或显式设置 `VT_DEVICE=cpu` 退回 CPU 模式。
3. **模型下载缓慢或连接超时**：
   - 设置 `HF_ENDPOINT=https://hf-mirror.com`，或手动将模型文件放置在 `models/large-v3/` 目录下。

---

## 1. FFmpeg / FFprobe（可执行工具）

| 工具 | 路径 | 说明 |
|---|---|---|
| ffmpeg | `F:\win-pyvideotrans-v3.92\ffmpeg\ffmpeg.exe` | 转码、loudnorm、silencedetect、抽流 |
| ffprobe | `F:\win-pyvideotrans-v3.92\ffmpeg\ffprobe.exe` | 探针：时长、音轨语言、volumedetect 音量画像、silencedetect 静音窗 |
| rubberband.exe | `F:\win-pyvideotrans-v3.92\ffmpeg\rubberband.exe` | 变调/变速（可选） |
| rubberband-r3.exe | `F:\win-pyvideotrans-v3.92\ffmpeg\rubberband-r3.exe` | rubberband 的 r3 变体 |
| sndfile.dll | `F:\win-pyvideotrans-v3.92\ffmpeg\sndfile.dll` | libsndfile，rubberband 依赖 |

**重要**：默认 `where ffmpeg` / `where ffprobe` 为空（不在 PATH）。任何依赖
ffmpeg 的命令前，必须先注入：

```powershell
$env:PATH = "F:\win-pyvideotrans-v3.92\ffmpeg;" + $env:PATH
```

验证：

```powershell
ffmpeg -version    # 应输出 ffmpeg version ...
ffprobe -version   # 应输出 ffprobe version ...
```

> `where ffmpeg` 为空 ≠ 没装。找不到时先读本文件登记路径注入 PATH，还不存在才
> 在常见盘（C:/D:/E:/F:）全盘搜 `ffmpeg.exe`，找到则补回本文件，下次直接复用。

---

## 2. CUDA 运行时库（GPU 推理必加的库目录）

本项目用 CTranslate2 后端跑 faster-whisper，`device=auto` 在本机（RTX 3070 Ti,
CUDA 12.x）会命中 GPU。**但 cuBLAS/cuDNN 库不在系统 PATH、也不在 venv 里**——
它们随工具链打包在：

| 库目录 | 路径 | 说明 |
|---|---|---|
| CUDA 12 libs | `F:\win-pyvideotrans-v3.92\_internal\torch\lib` | 含 `cublas64_12.dll` / `cublasLt64_12.dll` / `cudart64_12.dll` / `cudnn64_9.dll` 等，CTranslate2 GPU 推理必需 |

**漏加这个目录会报 `Could not load library cublas64_12.dll`**（曾经栽过的坑）。
开 GPU 推理时，PATH 必须**同时**含 FFmpeg 目录与 torch\lib：

```powershell
$env:PATH = "F:\win-pyvideotrans-v3.92\ffmpeg;F:\win-pyvideotrans-v3.92\_internal\torch\lib;" + $env:PATH
```

验证 GPU 可用：

```powershell
.venv\Scripts\python -c "from ctranslate2 import get_cuda_device_count; print(get_cuda_device_count())"
# 应输出 CUDA devices: 1
```

> 系统**未安装** CUDA Toolkit（`C:\Program Files\NVIDIA GPU Computing Toolkit`
> 不存在），所有 CUDA 运行时都来自上面的打包目录，必须注入 PATH。
> GPU 不可用时再退回 `--device cpu --compute-type int8`（慢）。

---

## 3. Python 运行时与项目虚拟环境

| 项 | 路径 | 说明 |
|---|---|---|
| 项目根 | `f:\workbuddy\github\video-translate` | 含 pyproject.toml |
| venv | `f:\workbuddy\github\video-translate\.venv` | Python 虚拟环境 |
| CLI 入口 | `f:\workbuddy\github\video-translate\.venv\Scripts\video-translate.exe` | 主命令 |
| python | `.venv\Scripts\python.exe` | 直接用 venv 的 python 跑工具脚本 |

激活 venv（PowerShell）：

```powershell
cd f:\workbuddy\github\video-translate
. .venv\Scripts\Activate.ps1
```

或始终用绝对路径调用：

```powershell
.venv\Scripts\video-translate.exe doctor
.venv\Scripts\python.exe -c "from video_translate.translate import validate_zh; ..."
```

---

## 3.1 依赖与 wheel 镜像（新增重依赖的标准做法）

> **本节约规（被 AGENTS.md §1 红线引用）**：以后加任何运行时依赖（尤其是
> `torch` / `torchvision` / `torchaudio` / `whisperx` / `pyannote` / `demucs`
> 这类重型 CUDA 包），**必须**按以下固定套路，避免重蹈「demucs 飘到系统 Python /
> 装成 CPU 版 / 没走镜像」的覆辙。

### 规则 1：依赖写进 `pyproject` 顶层 `dependencies`，不准藏 extra
- ❌ 不要放进 `[project.optional-dependencies]` 的 extra（如旧 `[audio]`），否则
  默认 `pip install -e .` 不装它。
- ✅ 直接写在 `dependencies = [...]` 里；`requirements.txt` 同步保留（去掉 OPTIONAL 注释）。
- 安装只跑一条命令：`pip install -e .` 或 `uv sync`，**不依赖任何额外动作**。

### 规则 2：CUDA wheel 必须走镜像索引，绝不裸装
- ✅ **首选 `uv sync`**：认 `pyproject` 的 `[tool.uv.sources]`，按平台 marker 自动
  选 wheel（Windows/Linux→清华 `cu124`，macOS→官方 `cpu`）。
- ✅ **用 pip 时**必须显式指定索引（pip 不读 `[tool.uv.*]`）：
  ```powershell
  $env:PIP_EXTRA_INDEX_URL = "https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cu124/"
  pip install -e .
  # 或重装 torch/torchaudio 时强制走 CUDA 源：
  pip install torch torchaudio --index-url https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cu124/
  ```
- ❌ 禁止裸 `pip install torch`（无代理时回退 PyPI 默认 `+cpu` wheel，GPU 失效）。

### 规则 3：镜像源固化进项目配置，不靠 Agent 临选
- CUDA 索引已写入 `pyproject` 的 `[tool.uv.index]`（cu124→清华镜像），`uv sync` 自动生效。
- `PIP_EXTRA_INDEX_URL` 作为 pip 用户的兜底，写在此文件 §镜像环境变量段。
- 安装一律"程序/配置决定"，任何 Agent/人工都不应在安装时现场拼镜像或挑代理。

### 验证装对了没有（装完必查）
```powershell
. .venv\Scripts\Activate.ps1
python -c "import torch, demucs; print(torch.__version__, torch.cuda.is_available())"
# 期望：版本带 +cu124（或 cu12x），cuda.is_available() == True（有卡机器）
```

---

## 4. 典型命令模板（已注入 PATH 后）

```powershell
# 1) 注入 ffmpeg + CUDA 库到 PATH（GPU 推理必须两块都加）
$env:PATH = "F:\win-pyvideotrans-v3.92\ffmpeg;F:\win-pyvideotrans-v3.92\_internal\torch\lib;" + $env:PATH

# 2) 进入项目并激活 venv
cd f:\workbuddy\github\video-translate
. .venv\Scripts\Activate.ps1

# 3) 自检环境
video-translate doctor

# 4) 跑管线（agent 引擎，默认）
video-translate run "videos\emily-blunt.mp4"

# 5) 生成字幕
video-translate generate `
    --segments videos\emily-blunt.segments_en.json `
    --zh videos\emily-blunt.zh_segments.json `
    --outdir videos\emily-blunt --base emily-blunt

# 6) 三 lane 自检门
video-translate verify `
    --segments videos\emily-blunt.segments_en.json `
    --zh videos\emily-blunt.zh_segments.json `
    --video videos\emily-blunt.mp4
```

---

## 5. 备注

- AGENTS.md §1 Preflight 的 `.venv/bin/video-translate` 是 Linux/macOS 写法；
  Windows 下为 `.venv\Scripts\video-translate.exe`（等价）。
- `doctor` 会报告 ffmpeg/ffprobe、HF 模型缓存、依赖、音频画像与 VAD 路由建议，
  开工前必须先跑（AGENTS.md 铁律）。
- 模型（large-v3）缓存在共享 `~\.cache\huggingface`，约 3GB，已下载则无需重下；
  也可放项目根 `models/large-v3/`（见 AGENTS.md 资产拖欠清单）。
