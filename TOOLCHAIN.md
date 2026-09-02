# 工具链与环境隔离指南 (TOOLCHAIN)

> 本文件是本项目关于**外部工具链（FFmpeg/FFprobe）、GPU/CUDA 运行时、Whisper 模型资产及环境配置（.env）**的唯一权威指引。
> 项目代码已支持**自动探测与环境注入**，通过 `.env` 系列配置文件实现跨平台与宿主环境解耦。
> **依赖管理 / 外部二进制 / 模型缓存 / CUDA 解析的「操作入口 + E1–E4 落地对照 + 新增工具标准套路」见 [`docs/TOOLING.md`](docs/TOOLING.md)**（里程碑 3 环境确定性工程专册）。

---

## 0. 环境模型总览（平台区分 × 开发/生产区分 × 目录四分离）

> 新读者（人类或 Agent）必读：本节回答三个高频问题——「不同操作系统怎么区分？」「开发环境和生产环境怎么区分？」「代码/环境/配置/数据各放哪？」。细节散落在 §1-§6 与 `pyproject.toml`，此处是唯一总览。

### 0.1 平台差异：三层全自动，零人为决策

| 层 | 机制 | 谁判断 |
|---|---|---|
| **① 依赖 wheel 层** | `pyproject` 的 `[[tool.uv.sources]]` 平台 marker：Windows/Linux → CUDA(**cu128**) wheel（官方 PyTorch 索引，torch 2.8 线 —— 由 `[gpu]` extra 的 whisperx 3.8.x 决定，见 ADR-028）；macOS → CPU wheel（官方源）。`uv.lock` 为**全平台统一 lockfile**（内含各平台版本+hash），`uv sync` 按当前机器自动取 | uv / pip（按 marker） |
| **② 环境变量层** | `.env`（全平台基础）→ `.env.win` / `.env.mac` / `.env.linux`（按 `sys.platform` 自动选）→ `.env.local`（单机覆盖） | `toolchain.py::get_platform_env_filename()` |
| **③ 运行设备层** | `VT_DEVICE=auto` → 有 NVIDIA 则 `cuda+int8_float16`，否则 `cpu+int8` 平滑降级（ADR-014）；8GB 显存机器强制 demucs→释放→Whisper 串行 | `transcribe.py::resolve_device()` |

平台差异被隔离在 gitignore 的本地 `.env.<platform>` 里；代码与 lockfile 保持全平台统一，**任何 Agent/人工不得临场拼平台分支**（R7）。

### 0.2 开发环境 vs 生产环境

本项目是 CLI + Agent 协议工具，区分在两个维度：

**依赖维度**（判定标准：用户跑 `uv run video-translate run` 会 import 到的 = 生产必装）：

| | 位置 | 安装 | 谁需要 |
|---|---|---|---|
| 运行时（生产） | `pyproject` 顶层 `[project.dependencies]` | `uv run video-translate setup` / `uv sync` / `pip install -e .` | 所有用户 |
| 开发依赖 | `[project.optional-dependencies].dev`（pytest 等） | `uv sync --extra dev` / `pip install -e ".[dev]"` | 贡献者/跑测试 |
| GPU 对齐（T4） | `[project.optional-dependencies].gpu`（whisperx，仅 Windows/Linux+CUDA） | `uv sync --extra gpu`（macOS 自动排除，零新依赖） | GPU 用户（`--align` 默认 `auto`，装好后自动启用 whisperx） |

**形态维度**：当前「源码即产品」——用户是 Agent + 开发者，同一台机器同一 `.venv`，dev 与 prod 靠下面的目录隔离而非独立部署；传统打包分发（PyInstaller 单 exe，见 `MAJOR_VERSION_PLAN.md` §5）为远期规划，启动前无需强分离。

### 0.3 目录四分离（环境可重建，数据永不误删）

```
git 跟踪   ：src/ tests/ docs/ pyproject.toml uv.lock   ← 代码与锁定（可 Review）
gitignore  ：.venv/            ← 环境（可随时删掉重建，uv run video-translate setup 几分钟）
             .env*             ← 机器配置（本机私有）
             videos/ outputs/  ← 用户数据（永不被安装/卸载/清理触碰）
             models/ tools/    ← 大资产（模型权重/便携 ffmpeg，可再生）
```

### 0.4 新机器标准工作流

```
① 先决条件：Python ≥ 3.10（唯一人工步骤；uv 未装见 https://docs.astral.sh/uv/getting-started/installation/）；
            ffmpeg 待 E2 落地后可 setup --ffmpeg 自动
② git clone && uv run video-translate setup      # uv sync 依赖（uv.lock 固化，按平台自动选源）+ 模型（项目根 models/，零 C 盘，E3 后带自愈）
③ cp .env.<platform>.example .env.<platform>  # 填本机差异项（E4 后 CUDA 通常免填）
④ uv run video-translate doctor                  # 全绿才算就绪（实际命令：uv run video-translate doctor）
⑤ Agent 按 AGENTS.md 状态机开工（run → exit 6 → 翻译 → generate → verify）
故障恢复：删 .venv 重跑 uv run video-translate setup（数据无损）
```

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
- `PIP_EXTRA_INDEX_URL`：PyTorch wheel 镜像（`uv sync` 已通过 `pyproject.toml` 的
  `[[tool.uv.index]]` + `[[tool.uv.sources]]` 按平台自动选源，无需手动设；仅当用 `pip`
  兜底安装时才需设此环境变量，CUDA 12.8 例（与 `pyproject` 的 cu128 保持一致）：
  `https://download.pytorch.org/whl/cu128/`；CN 无代理可用清华
  `https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cu128/`）。
- `VT_DEVICE`：计算设备（`auto` / `cuda` / `cpu`）。
- `VT_COMPUTE_TYPE`：量化类型（`auto` / `int8_float16` / `int8` / `float16`）。
- `VT_ENGINE`：翻译引擎（默认 `agent`，可选 `google`）。
- `VT_PROXY`：HTTP 代理地址（如 `http://127.0.0.1:7890`，仅在 `--engine google` 时使用）。
- `VT_STYLE`：翻译风格轨（默认 `film`，可选 `literal` / `bilingual_study`，可逗号多轨；T3 / ADR-027）。
- `VT_ALIGN`：词级强制对齐后端（默认 `auto`：GPU + whisperx 可用走 whisperx，否则 `none`；可选 `none` / `whisperx`；T4 / ADR-028，对齐后端安装与契约见 §2.6）。
- `VT_SEPARATE_VOCALS`：是否先用 Demucs 剥离纯人声再转写（默认 `false`；T2 / ADR-017）。
- `VT_DEMUCS_MODEL`：Demucs 人声分离模型（默认 `htdemucs`；T2 高级参数）。

### 1.3 命令入口（一律 uv run）

本项目所有 CLI / 工具脚本调用**一律加 `uv run` 前缀**，由 `uv` 自动定位项目 `.venv`，
杜绝命中 PATH 里残留的旧全局 Python（如 `F:\Python311`，缺 whisperx）导致 `doctor` 误报、反复排障。

- ✅ 标准：`uv run video-translate <subcommand>`、`uv run python -c "..."`、`uv run pytest`。
- ❌ 禁止：裸 `python` / `video-translate` / `make` 指望 PATH 指向项目环境；手动 `.venv\Scripts\Activate.ps1` 激活 venv。
- 开新终端先 `cd <repo>` 再加 `uv run` 前缀；`uv` 未装见 [官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)。
- 详情见 [Spec 23 环境定位](docs/specs/23-environment-location.md) / [ADR-029](docs/adr/029-command-entry-uv-run.md)。

---

## 2. 外部依赖工具链详情

外部工具链由以下组件构成：

### 2.1 FFmpeg / FFprobe（音视频处理核心）
- **作用**：音频抽流（16kHz 单声道 WAV）、音量与静音画像（`volumedetect`、`silencedetect`）、时长探测。
- **确定性获取（首选，Milestone 3 / E2）**：`doctor` 报 ffmpeg 缺失时，**不要**全盘搜或手动安装，直接跑：
  ```bash
  uv run video-translate setup --ffmpeg
  ```
  它会按当前平台下载便携版（Windows gyan.dev zip / Linux johnvansickle 静态 tar.xz / macOS evermeet zip）解压到 `tools/ffmpeg/bin`，并写入 `.env.local`（`gitignore`，单机私有）。全程幂等，重跑不重复下载。
- **探测顺序**（两步，无"全盘搜"自由发挥）：
  1. **系统 PATH**：`ffmpeg -version` 可直接运行即用。
  2. **`VT_FFMPEG_DIR` 配置**：否则读取 `.env` / `.env.<platform>` / `.env.local` 中的 `VT_FFMPEG_DIR`，程序自动注入运行时 PATH。该变量可由 `setup --ffmpeg` 自动写入 `.env.local`。
- 两步皆无 → 判为缺失，`doctor` 默认以 `EXIT_DOCTOR_FAIL(7)` 退出并打印 `[FIX] uv run video-translate setup --ffmpeg`，**禁止** Agent 自行全盘搜索或散落安装（ffmpeg/ffprobe 是核心流水线硬依赖，缺之转写必崩，故默认即闸，不依赖 `--strict`）。

### 2.2 CUDA 运行时库（GPU 推理加速）
- **作用**：faster-whisper 基于 CTranslate2 后端，在 NVIDIA GPU 下可实现 5-10x 实时加速。
- **动态库依赖**：需要 `cublas64_12.dll`、`cublasLt64_12.dll`、`cudart64_12.dll`、`cudnn64_9.dll` 等。
- **解析顺序（Milestone 3 / E4，确定性，不散落缓存）**：
  1. **① venv torch/lib（自动探测）**：`init_toolchain` 通过 `import torch` 定位当前 venv 内
     随装的 `torch/lib` 目录，**默认即生效，无需任何 `.env` 配置**。`doctor` 会标注来源
     `venv-torch`。
  2. **② 显式覆盖 `VT_CUDA_DIR` / `VT_TORCH_LIB_DIR`**：仍最高优先，磁盘上存在才取用，适合指向
     系统 CUDA Toolkit 或绿色版运行库。来源标注 `env`。
  3. **③ `CUDA_PATH`**：系统 CUDA Toolkit 环境变量。来源标注 `system`。
  4. **④ 无**：CPU 自动降级（`--device cpu --compute-type int8`）。
- **配置方式**：
  - 绝大多数情况**什么都不用配**：本 venv 的 torch 自带 CUDA 运行库，`doctor` 显示来源 `venv-torch`。
  - 仅在需覆盖时，在 `.env.win` 指定 `VT_CUDA_DIR`（例：`VT_CUDA_DIR=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin`）。
- **CPU 自动回退机制**：
  - 当 `VT_DEVICE=auto` 时，系统先探测 CUDA。若无 GPU 或 CUDA 依赖库缺失/加载失败，系统会自动降级为 `--device cpu --compute-type int8` 运行，不会直接崩溃中断。

### 2.3 Whisper 模型（large-v3，约 3GB）
- **本地优先原则（默认零 C 盘缓存）**：
  - 程序启动 / 下载时，优先使用项目根目录 `models/large-v3/`（需含 `model.bin`）。若存在，
    **直接离线加载，完全不依赖网络，也不读写 `C:\Users\...\AppData`**。
  - `uv run video-translate setup` 默认把模型下载到项目根 `models/large-v3/`（而非系统 HF 缓存目录），
    所以**模型始终留在仓库内、可随项目拷贝、不污染用户目录**。
  - 仅当项目根 `models/` 缺失且未设置 `HF_HOME` 时，才会回退到系统共享缓存
    `~/.cache/huggingface/hub/`。
- **想彻底不碰 C 盘用户目录**：只要把完整模型包（含 `model.bin` 等）放到 `<repo>/models/large-v3/`，
  程序即离线加载；或设 `HF_HOME=D:\hf_cache` 把任何回退缓存也挪到非系统盘。
- **完整性校验 + 自愈（Milestone 3 / E3）**：
  - 判定"已缓存"不仅看 `model.bin` 是否存在，还要看其大小 ≥ 2 GiB 下限。一个被截断的
    `model.bin`（如中断下载只下了几百 MB）**不再被误判为已缓存**。
  - `uv run video-translate setup` 在下载前会自动删除所有"存在但小于下限"的 `model.bin`（本地 `models/`
    与 HF 共享 cache 均扫），随后重新拉取完整权重到 `models/`，实现自愈。
  - 若转写阶段加载失败（缓存损坏），`run` 捕获后以退出码 `EXIT_MISSING_DEP(3)` 退出并打印：
    `fix: uv run video-translate setup`，不会抛出裸 traceback。
- **网络镜像与离线下载**：
  - 如需在线拉取，在 `.env` 中设置 `HF_ENDPOINT=https://hf-mirror.com`，然后执行 `uv run video-translate setup`。
  - 无网络环境下，用户可从镜像源下载完整模型包并解压至项目根 `models/large-v3/`（必须含完整 `model.bin`）。

### 2.4 Demucs 语音分离模型（htdemucs，约 400MB+，可选 T2 预处理）
- **作用**：人声/伴奏分离（T2 层）。模型由 demucs 经 `torch.hub` 下载，默认会落到
  `C:\Users\<user>\.cache\torch\hub\checkpoints\`（系统盘）——**本项目已改为项目本地优先**。
- **项目本地优先（Milestone 3 后规范，零 C 盘）**：
  - `vocal_sep.py` 在调用 demucs 前，把 `TORCH_HOME` 绑定到 `<repo>/models/torch`，
    因此 htdemucs 权重**下载并缓存到 `<repo>/models/torch/`，完全不进 C 盘用户目录**。
  - 该行为对所有调用 `separate_vocals` 的路径自动生效，无需用户配置。
- **仅当显式设了 `TORCH_HOME` 指向别处**才会改变落点；默认即项目内，符合 §6 规范。

### 2.5 依赖与 wheel 镜像（新增重依赖的标准做法）

> **本节约规（被 AGENTS.md §1 红线引用，标题「§依赖与 wheel 镜像」）**：以后加任何运行时依赖（尤其是
> `torch` / `torchvision` / `torchaudio` / `whisperx` / `pyannote` / `demucs`
> 这类重型 CUDA 包），**必须**按以下固定套路，避免重蹈「demucs 飘到系统 Python /
> 装成 CPU 版 / 没走镜像」的覆辙。
> **完整规则（R1-R7）固化于 [MAJOR_VERSION_PLAN.md](MAJOR_VERSION_PLAN.md) §3.2**，
> 本节为其在安装操作层面的落地说明；两处修订须同步。

**规则 1：依赖写进 `pyproject` 顶层 `dependencies`，不准藏 extra**
- ❌ 不要放进 `[project.optional-dependencies]` 的 extra（如旧 `[audio]`），否则
  默认 `pip install -e .` 不装它。
- ✅ 直接写在 `dependencies = [...]` 里；`requirements.txt` 同步保留（去掉 OPTIONAL 注释）。
- 安装只跑一条命令：`uv run video-translate setup`（默认 `uv sync`，uv 不可用时回退 `pip install -e .`），**不依赖任何额外动作**。

**规则 2：CUDA wheel 必须走镜像索引，绝不裸装**
- ✅ **首选 `uv run video-translate setup` / `uv sync`**：认 `pyproject` 的 `[[tool.uv.sources]]`，按平台 marker 自动
  选 wheel（Windows/Linux→官方 `cu128`，macOS→官方 `cpu`）。
- ✅ **用 pip 兜底时**必须显式指定索引（pip 不读 `[tool.uv.*]`，版本须与 `pyproject` 一致）：
  ```powershell
  $env:PIP_EXTRA_INDEX_URL = "https://download.pytorch.org/whl/cu128/"
  pip install -e .
  # 或重装 torch/torchaudio 时强制走 CUDA 源：
  pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128/
  ```
- ❌ 禁止裸 `pip install torch`（无代理时回退 PyPI 默认 `+cpu` wheel，GPU 失效）。

**规则 3：镜像源固化进项目配置，不靠 Agent 临选**
- CUDA 索引已写入 `pyproject` 的 `[[tool.uv.index]]`（**cu128** 官方 PyTorch 索引），`uv sync` 自动生效。
  两个 PyTorch 索引都标了 `explicit = true`：它们只服务 `[[tool.uv.sources]]` 里点名的
  torch/torchaudio，不会用陈旧版本遮蔽 PyPI 上的通用包（否则 tqdm 之类会被锁死在
  cu128 索引里那个过低的版本上，导致解析无解）。
- `PIP_EXTRA_INDEX_URL` 作为 pip 用户的兜底，写在此文件 §1.2 环境变量段。
- 安装一律"程序/配置决定"，任何 Agent/人工都不应在安装时现场拼镜像或挑代理。

**验证装对了没有（装完必查）**
```bash
uv run python -c "import torch, demucs; print(torch.__version__, torch.cuda.is_available())"
# 期望：版本形如 2.8.0+cu128，cuda.is_available() == True（有卡机器）
```

### 2.6 WhisperX 词级对齐后端（T4，可选 GPU）
- **作用**：在 faster-whisper 转写结果上做词级时间戳精修（forced alignment），产出字幕所需的精确词边界。默认 `--align auto`：检测到 GPU + whisperx 可用时自动启用，否则降级为 `none`（转写仍正常，仅无词级时间戳）。后端选型决策见 **ADR-028**，受 T4 里程碑约束。
- **归属与安装**：whisperx 仅 Windows/Linux+CUDA 需要，放在 `pyproject` 的 `[project.optional-dependencies].gpu` extra（与 torch 的 cu128 wheel 同链，由 whisperx 3.8.x 决定 CUDA 线，见 §0.1）；安装走 `uv sync --extra gpu`（macOS 自动排除，零新依赖）。它是**运行时对齐依赖**，必须随 `[gpu]` extra 进入 `uv.lock`，不得藏进未默认安装的 extra。
- **对齐契约**：CLI 三级覆盖、缓存命名、降级矩阵与不变量见 **Spec 22**（T4 行为契约）。
- **验证**：`uv run video-translate run` 后观察日志 `align=whisperx`；CPU / 未装 whisperx 时应为 `align=none`（不报错、不崩溃）。

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
2. 在 `.env.win`（或对应文件）中按需填入实际路径。多数情况**留空即可**——torch 自带 CUDA 运行库会被自动探测，FFmpeg 可用 `uv run video-translate setup --ffmpeg` 拉取。仅在覆盖时填：
   ```dotenv
   # 留空 = 自动探测 venv torch/lib（GPU）或 CPU 降级
   VT_FFMPEG_DIR=
   VT_CUDA_DIR=
   ```
3. 运行环境自检：
   ```bash
   uv run video-translate doctor
   ```
   若输出中 `ffmpeg`、`ffprobe`、`large-v3 model` 均为 `[OK]`，且 `device` 处于预期状态，说明工具链初始化成功。

---

## 4. 常用执行命令

```bash
# 1. 预检
uv run video-translate doctor

# 2. 跑管线（默认 agent 引擎，转写完成后输出 translate_task.json 并返回 exit code 6）
uv run video-translate run "videos/example.mp4"

# 3. 生成最终字幕文件
uv run video-translate generate --segments videos/example.segments_en.json --zh videos/example.zh_segments.json --outdir videos/example --base example

# 4. 门禁验证
uv run video-translate verify --segments videos/example.segments_en.json --zh videos/example.zh_segments.json --video videos/example.mp4
```

---

## 5. 常见问题排查 (Troubleshooting)

1. **报 `ffmpeg/ffprobe not found in PATH`**：
   - 检查 `.env` 或 `.env.win` 中的 `VT_FFMPEG_DIR` 是否指向包含 `ffmpeg.exe` 的文件夹。
   - 运行 `uv run video-translate doctor` 查看 `env config` 行是否成功加载了配置文件。
2. **报 `Could not load library cublas64_12.dll`**：
   - 说明 GPU 模式被激活，但缺少 CUDA 12 动态库。
   - 在 `.env.win` 中补充 `VT_CUDA_DIR` 指向包含该 dll 的目录，或显式设置 `VT_DEVICE=cpu` 退回 CPU 模式。
3. **模型下载缓慢或连接超时**：
   - 设置 `HF_ENDPOINT=https://hf-mirror.com`，或手动将模型文件放置在 `models/large-v3/` 目录下。

### 备注
- AGENTS.md §1 Preflight 的 `.venv/bin/video-translate` 是 Linux/macOS 写法；
  Windows 下等价调用为 `uv run video-translate`（由 `uv` 定位项目 `.venv`）。
- `doctor` 会报告 ffmpeg/ffprobe、HF 模型缓存、依赖、音频画像与 VAD 路由建议，
  开工前必须先跑（AGENTS.md 铁律）。ffmpeg/ffprobe 缺失时 `doctor` 默认 `EXIT_DOCTOR_FAIL(7)` 退出（硬依赖，缺之转写必崩）；其余项默认仅打印 `[MISS]`，`--strict` 才把所有 `[MISS]` 升格为失败。
- 模型（large-v3）**默认落项目根 `models/large-v3/`**（零 C 盘，见 §6 规范）；
  仅当项目根缺失且未设 `HF_HOME` 时才回退 `~\.cache\huggingface`（见 §2.3）。

---

## 6. 工具与依赖管理规范（零 C 盘 / 项目本地优先）

> **这是本项目的硬性管理规范**，被 AGENTS.md §1 红线引用。以后**新增任何工具、
> 依赖或模型**，都必须遵循本节，不得把产物落到系统盘用户目录
> （`C:\Users\<user>\.cache`、`C:\Users\<user>\AppData`、`C:\Users\<user>\torch` 等）。

### 6.1 总原则
1. **一切可再生的重量级产物（模型权重、下载的工具链、第三方缓存）都落在仓库目录内**，
   通过 `.gitignore` 排除，可随项目拷贝、不污染系统、不依赖某台机器的用户目录。
2. **落点统一约定**（空环境首次 `uv run video-translate setup` 后的最终状态，Windows）：

   | 组件 | 落点 | 说明 |
   |---|---|---|
   | Python 依赖 | `<repo>/.venv/` | `uv sync` 创建，gitignore |
   | Whisper 模型 large-v3 | `<repo>/models/large-v3/` | 本地优先，`setup` 下载到此；gitignore |
   | Demucs 语音分离模型 htdemucs | `<repo>/models/torch/` | 经 `TORCH_HOME` 绑定到项目内（torch.hub 缓存）；gitignore |
   | FFmpeg 便携版 | `<repo>/tools/ffmpeg/bin/` | `setup --ffmpeg` 下载；gitignore |
   | CUDA 运行库 | venv 内 `torch/lib` 或 `VT_CUDA_DIR` 指向的系统目录 | 随 `uv sync` 装 torch 一并就绪，不单独下载 |
   | 中间产物（vocals/demucs_out/转写 json/字幕） | `--outdir` 指定目录 | 默认视频同级 |

3. **回退规则**：仅当项目内路径缺失且未设 `HF_HOME`/`TORCH_HOME` 等环境变量时，
   才允许回退到系统用户目录；但**默认配置与 `setup` 流程必须把它们引回项目内**。

### 6.2 新增工具/依赖的标准套路
- **Python 依赖**：写进 `pyproject` 顶层 `dependencies`（见 §2.5），`uv run video-translate setup` 一条命令装齐，
  随 venv 落在 `<repo>/.venv/`，不进系统 Python（避免 demucs 飘到系统 Python 的历史问题）。
- **需下载的模型/权重**：
  - 优先支持「项目根 `models/<name>/` 本地 drop-in」+「`setup` 下载到项目内」双路径（参照
    Whisper 的 `_resolve_model_path` / `_LOCAL_MODEL_DIR` 写法）。
  - 若底层库走 `torch.hub` / `HF Hub`，在调用前**绑定 `TORCH_HOME` / `HF_HOME` 到
    `<repo>/models/...`**（参照 `vocal_sep.py::_bind_demucs_cache`），而非依赖默认 C 盘路径。
  - 下载逻辑必须**幂等**、**走代理**，残缺时自愈（见 §2.3 E3 规则）。
- **需下载的工具（如 FFmpeg）**：实现 `ensure_*` 函数，按平台选源、解压到 `<repo>/tools/...`、
  写入 `.env.local`（gitignore），`doctor` 在缺失时提示确定性修复命令（见 §2.1 E2 规则）。
- **禁止**：裸 `pip install <heavy>` 装到系统 Python；让 Agent 现场全盘搜索或散落安装；
  把模型/缓存写到 `C:\Users\...`。

### 6.3 验证（新增后必查）
```powershell
# 1) 确认没有产物落到 C 盘用户目录
#    Whisper: 应在 <repo>/models/large-v3/
#    Demucs : 应在 <repo>/models/torch/（TORCH_HOME 指向它）
Get-ChildItem "C:\Users\$env:USERNAME\.cache" -ErrorAction SilentlyContinue
Get-ChildItem "C:\Users\$env:USERNAME\.torch" -ErrorAction SilentlyContinue
# 2) doctor 全绿
uv run video-translate doctor
# 3) 跑一条最短链路，确认产物落在 --outdir 而非系统盘
```

---

## 附录 A. 本机工具链实况（以 `uv run video-translate doctor` 输出为准）

> 本节是**本机历史实况快照**，仅供排障参考。E2/E4 落地后，PATH 注入由
> `init_toolchain` 在程序启动时**自动完成，无需手动执行**。新机器不要照抄本节
> 路径，统一走 `uv run video-translate setup` + `uv run video-translate setup --ffmpeg`（见
> [docs/TOOLING.md](docs/TOOLING.md)）。

### A.1 FFmpeg / FFprobe

| 工具 | 作用 |
|---|---|
| ffmpeg | 转码、loudnorm、silencedetect、抽流 |
| ffprobe | 探针：时长、音轨语言、volumedetect 音量画像、silencedetect 静音窗 |

- 启动时由 `init_toolchain` 按「① 系统 PATH → ② `.env` 登记 `VT_FFMPEG_DIR`」顺序**自动注入**。
- 新机器 ffmpeg 缺失：`uv run video-translate setup --ffmpeg` 自动下载便携版到 `tools/ffmpeg/` 并写 `.env.local`（E2）。
- `where ffmpeg` 为空 ≠ 没装；验证以 `uv run video-translate doctor` 显示 ffmpeg/ffprobe `[OK]` 为准。

---

### A.2 CUDA 运行时库

本项目用 CTranslate2 后端跑 faster-whisper，`device=auto` 在本机（RTX 3070 Ti,
CUDA 12.x）会命中 GPU。**E4 后 CUDA 库目录解析顺序（自动，无需手动注入 PATH）**：

1. **venv 内 `torch/lib`** —— `uv sync` 装的 cu128 wheel（torch 2.8 线）自带完整 CUDA
   运行时（cublas/cudnn），自动探测，`doctor` 标注 `venv-torch`；
2. **`VT_CUDA_DIR` / `VT_TORCH_LIB_DIR`** —— 显式覆盖（最高优先级，仅 venv 内
   缺 DLL 的特殊场合手填）；
3. 都没有 → **静默 CPU 降级**（`--device cpu --compute-type int8`），不崩溃。

**曾经栽过的坑**（历史）：E4 之前借外部项目 `torch\lib` 目录，PATH 漏加会报
`Could not load library cublas64_12.dll`。E4 已根治——CUDA 运行时随 venv 内
torch wheel 一起安装，`uv run video-translate setup` 后即就位。

验证 GPU 可用：

```powershell
uv run video-translate doctor     # CUDA source: venv-torch / env / none
uv run python -c "from ctranslate2 import get_cuda_device_count; print(get_cuda_device_count())"
# 应输出 CUDA devices: 1
```
