# TOOLING.md — 工具与依赖管理专册

> 本文件是「依赖 / 外部二进制 / 模型缓存 / CUDA / 镜像代理」的**集中操作入口 + 规范**。
> 详细技术实现见 [`../TOOLCHAIN.md`](../TOOLCHAIN.md)；任务原始定义见 [`../MAJOR_VERSION_PLAN.md`](../MAJOR_VERSION_PLAN.md) 的 E1–E4。
> 本专册对应里程碑 3（环境确定性工程），落地内容：**E1 uv.lock 可复现 / E2 ffmpeg 自动下载 / E3 模型缓存校验自愈 / E4 CUDA venv 优先**。

---

## 0. 一图看懂（给小白 / 新 Agent）

```
新 clone 项目
   │
   ├─ make setup            ← ① uv sync（依赖，uv.lock 固化）+ ② 预拉 Whisper 模型到 <repo>/models/
   │
   ├─ make doctor           ← 全绿校验：ffmpeg / CUDA·CPU / 模型缓存
   │     ├─ ffmpeg [MISS]  → video-translate setup --ffmpeg   （E2 自动下载便携版，非全盘搜）
   │     ├─ 模型  [MISS]   → make setup / video-translate setup（E3 完整性校验 + 残缺自愈）
   │     └─ CUDA 来源标注   → venv-torch / env / none           （E4 自动探测 venv torch/lib）
   │
   └─ video-translate run <视频>   ← 转写（本地，不触网）→ 等待 Agent 翻译 → 生成字幕
```

**关键原则**：环境必须一步到位、禁止自由发挥配环境（见 `AGENTS.md` Phase 0）。所有就绪动作统一走 `make setup` / `setup --ffmpeg`，**不散落缓存、不全盘搜、不手动改路径**。

---

## 1. E1 — uv.lock 可复现安装【P0】

### 1.1 任务定义（来自 MAJOR_VERSION_PLAN E1）
- **问题**：仓库无 lockfile，换机安装存在版本飘移；「依赖装错环境 / 装成 CPU 版」红线事故无法根治。
- **验收**：新 clone `make setup` 一次成功；`uv lock --check` 通过；Win/Linux 装出 `+cu124` torch，macOS 装出 CPU torch；全量 pytest 绿。

### 1.2 落地实现
| 项 | 落点 | 说明 |
|---|---|---|
| lockfile | `uv.lock`（仓库根，已提交，不 gitignore） | `uv` 生成；任何 `pyproject` 依赖变更必须同 commit 重跑 `uv lock` |
| 安装主路径 | `Makefile` 的 `setup` 目标 = `uv sync --extra dev`（缺 dev 时回退 `uv sync`） | 未装 `uv` 时自动回退 `pip install -e .` 并打印安装指引 |
| 镜像索引 | `pyproject.toml` 的 `[tool.uv.index]`（清华 cu124）+ `[tool.uv.sources]`（按平台选 wheel） | CUDA wheel 只走镜像，绝不裸装（R2） |
| 文档口径 | `TOOLCHAIN.md` §3.1 + `README.md` Quickstart | 统一为「uv sync 标准、pip 兜底」 |

### 1.3 依赖变更标准动作（R1 + R3）
```bash
# 改了 pyproject.toml 的 dependencies 后，必须成对执行：
uv lock              # 重算 uv.lock
git add pyproject.toml uv.lock && git commit   # 同 commit 提交
```
**红线**：只改 `pyproject` 不提交 `uv.lock` → 换机版本飘移。运行时依赖一律进顶层 `dependencies`，不藏 extra（R1）。

### 1.4 验证
```bash
uv lock --check          # 必须 0 退出
python -c "import torch; print(torch.__version__)"   # Win/Linux 应含 +cu124
```

---

## 2. E2 — ffmpeg 自动下载便携版（消灭「全盘搜」）【P0】

### 2.1 任务定义（来自 MAJOR_VERSION_PLAN E2）
- **问题**：旧协议「全盘搜 C:/D:/E:/F: 找 ffmpeg.exe」是最不确定的步骤，`make setup` 也不覆盖 ffmpeg。
- **验收**：无 ffmpeg PATH 的环境跑 `setup --ffmpeg` 后 `doctor` 全绿；单测覆盖下载 mock / 解压 / `.env.local` 写入 / 幂等。

### 2.2 落地实现
| 项 | 落点 | 说明 |
|---|---|---|
| 下载函数 | `src/video_translate/toolchain.py` 的 `ensure_ffmpeg(dest="tools/ffmpeg")` | 按 `sys.platform` **先选平台再选源**：Win→gyan.dev release-full(zip)；Linux→静态构建(tar.xz)；macOS→evermeet/镜像(zip)。走 `proxy.detect_proxy` |
| CLI 旗标 | `cli.py` 的 `setup` 子命令 `--ffmpeg` | 缺失时下载、解压至 `tools/ffmpeg/`，并把 `VT_FFMPEG_DIR` 写入 `.env.local`（gitignore，机器私有） |
| doctor 提示 | ffmpeg `[MISS]` → 打印 `run: video-translate setup --ffmpeg` | Agent 看到即知跑哪条 |
| 探测收敛 | `TOOLCHAIN.md` §2.1 三步→两步：① 系统 PATH → ② `setup --ffmpeg` | 「全盘搜」已删除，仅作人工兜底不再写协议 |
| 二进制不进 git | `tools/` 加入 `.gitignore` | 与 `models/` 同理 |

### 2.3 给小白 / Agent 的操作
```bash
video-translate setup --ffmpeg     # 自动下载便携版到 <repo>/tools/ffmpeg/
video-translate doctor             # 确认 ffmpeg/ffprobe [OK]
```
**不要再**：手动搜各盘 `ffmpeg.exe`、手动下载塞进仓库。

---

## 3. E3 — 模型缓存完整性校验 + 自愈【P1】

### 3.1 任务定义（来自 MAJOR_VERSION_PLAN E3）
- **问题**：旧 `_model_cached` 只查 `model.bin` 是否存在——下载中断的残缺文件被误判已缓存，`run` 在转写深处崩溃，报错不友好。
- **验收**：残缺缓存（<2GB 假 model.bin）被检出并自愈重下；`run` 阶段加载失败输出含修复命令；正常缓存幂等不受影响。

### 3.2 落地实现（项目本地优先，零 C 盘）
| 项 | 落点 | 说明 |
|---|---|---|
| 模型落点 | 默认 `<repo>/models/large-v3/`（含 `model.bin`） | 随项目拷贝、不读写 `C:\Users\...`；仅当项目根缺失才回退 `HF_HOME`（R5） |
| 完整性校验 | `cli.py` 的 `_model_cached` | 不仅看存在，还要求 `model.bin` 大小 ≥ 2 GiB（large-v3 完整 ≈ 3.09GB） |
| 自愈 | `cmd_setup` | 下载前自动删除「存在但 < 下限」的 `model.bin`（项目 `models/` 与 HF 共享 cache 均扫），随后重拉完整权重 |
| 加载失败兜底 | `transcribe.py` 的 `WhisperModel(...)` 包异常捕获 | 失败时打印 `fix: video-translate setup`，以 `EXIT_MISSING_DEP(3)` 退出，不裸 traceback |
| 镜像 / 离线 | `HF_ENDPOINT=https://hf-mirror.com` + `setup`；或手动解压完整包到 `<repo>/models/large-v3/` | 无网络时支持离线 drop-in |

### 3.3 操作
```bash
make setup                              # 预拉模型到 <repo>/models/（断点可重跑，自愈残缺）
video-translate setup                   # 同上
# 若 run 报「模型加载失败」：
video-translate setup                   # 自愈重下
```

---

## 4. E4 — CUDA 解析顺序：venv torch/lib 优先【P1】

### 4.1 任务定义（来自 MAJOR_VERSION_PLAN E4）
- **问题**：CUDA DLL 旧依赖 `.env.win` 硬编码外部项目路径（`F:\win-pyvideotrans-v3.92\_internal\torch\lib`）。新机器没装过 pyvideotrans 则 GPU 不可用。
- **验收**：有 GPU + venv cu124 torch 的机器**不配** `VT_CUDA_DIR` 也能 `device=cuda`；显式 `VT_CUDA_DIR` 仍优先；无 torch/DLL 静默 CPU 降级；`doctor` 标注来源。

### 4.2 落地实现（`toolchain.py` `init_toolchain`）
**CUDA 目录解析顺序（确定性）**：
1. **venv 内 `torch/lib`** — 自动探测：`import torch; torch.__file__` 定位 venv 内 `site-packages/torch/lib`（PyTorch cu1xx wheel 自带完整 CUDA 运行时）。
2. **`VT_CUDA_DIR` / `VT_TORCH_LIB_DIR`** — 显式覆盖（**仍最高优先级**，用户明确指定时不抢夺）。
3. **无则 CPU 降级** — 不崩溃。

`doctor` 输出 CUDA 来源标注：`venv-torch` / `env` / `none`。

### 4.3 操作
```bash
# 绝大多数情况什么都不用配：venv torch 自带 CUDA，doctor 显示 venv-torch
video-translate doctor      # CUDA source: venv-torch
# 仅在覆盖时（venv torch/lib 缺 DLL）：
#   .env.win 设 VT_CUDA_DIR=C:\...\CUDA\v12.4\bin
```
`.env.win.example` 已移除 pyvideotrans 硬编码，注明「通常无需配置」。

---

## 5. 依赖与外部资产总规范（R1–R7 速查）

| # | 规则 | 正例 |
|---|---|---|
| R1 | 运行时依赖进 `pyproject` 顶层 `dependencies`；dev 工具进 `[optional-dependencies].dev` | demucs/torchaudio 在顶层 |
| R2 | CUDA wheel 只走镜像索引，绝不裸装；索引版本须与 `pyproject` 一致（当前 **cu128** / torch 2.8 线） | `uv sync` / `pip install --index-url …cu128/` |
| R3 | `uv.lock` 是依赖唯一事实来源，与 `pyproject` 成对变更 | 改依赖必重跑 `uv lock` |
| R4 | 外部二进制（ffmpeg）不手动装、不进 git，统一 `setup --ffmpeg` 下载到 `tools/`（gitignore） | `video-translate setup --ffmpeg` |
| R5 | 模型权重不进 git，**项目本地优先（零 C 盘）** 落 `<repo>/models/<name>/`；`HF_HOME` 仅回退覆盖；过完整性校验（E3） | `make setup` 拉模型到 `<repo>/models/` |
| R6 | 新依赖准入清单：跨平台 / 缓存指纹 / 显存预算(8GB) / 等价实现 / lockfile 同步 | 每项写入对应任务 Spec |
| R7 | 镜像/代理固化在配置，不靠临场决策 | PyPI/PyTorch 进 `[tool.uv.index]`；HF 进 `HF_ENDPOINT` |

---

## 6. 新增一个外部工具 / 依赖的标准套路

> 任何引入新二进制（如 ffmpeg 之后再加 xxx）或新 Python 依赖时，照此清单执行，避免重蹈「散落缓存 / 版本飘移」覆辙。

1. **Python 依赖**：写进 `pyproject` 顶层 → `uv lock` → 同 commit 提交 lockfile（R1+R3）。
2. **外部二进制**：
   - 在 `toolchain.py` 加 `ensure_<tool>(dest="tools/<tool>")`：按平台选源 + 走代理 + 幂等（已存在跳过）。
   - `cli.py` 的 `setup` 加 `--<tool>` 旗标；`doctor` 缺失时打印 `run: video-translate setup --<tool>`。
   - `tools/<tool>/` 加入 `.gitignore`（R4）。
   - `TOOLCHAIN.md` 的探测步骤收敛为「PATH → setup 自动下载」，删掉任何「全盘搜」步骤（E2 先例）。
3. **模型权重**：默认落 `<repo>/models/<name>/`（零 C 盘），加 ≥ 下限的完整性校验 + 自愈（E3 先例，R5）。
4. **语料 / 数据资产**（如 whisperx 对齐所需的 NLTK `punkt` / `punkt_tab`）：
   落 `<repo>/models/nltk_data/`，`setup --align` 幂等下载，`doctor` 缺失即打印修复
   命令；**代码侧在调用前主动把该目录注册进库的搜索路径**（`register_nltk_path()`
   写入 `nltk.data.path`）。这样运行时不依赖 `NLTK_DATA` 之类的环境变量，且
   `doctor` 与真实运行看到同一份状态 —— 否则会出现「doctor 报缺失、实际却能用」
   的假告警（踩过）。关键性不亚于二进制：whisperx 缺语料时抛的 LookupError 会被
   逐段回退吞掉，表现为「对齐跑完了但一个时间戳都没变」。
5. **CUDA / 运行时库**：优先自动探测 venv 内自带库（venv-torch），显式 env 变量
   `VT_CUDA_DIR` 作覆盖，其次才是系统 `CUDA_PATH`；每个候选项都必须**实际含有
   CUDA DLL** 才算命中（E4 先例 —— 否则 `CUDA_PATH=F:\Program Files` 这类无关
   目录会被当成 CUDA 目录）。
6. **文档同步**：本专册 + `TOOLCHAIN.md` + `AGENTS.md` 红线表同步更新；跨平台降级路径必须写清。

---

## 7. 验证清单（CI / 人工）

```bash
make setup            # uv sync + 模型预拉，一次成功
uv lock --check       # 0 退出
video-translate doctor  # ffmpeg [OK] / CUDA source: venv-torch / 模型 [OK] / whisperx OK
pytest                # 全量绿（E2/E3/E4 均有 mock 单测覆盖，不真联网/不真下 3GB）
```
- E2 单测：`tests/test_toolchain.py`（下载 mock、zip 解压、`.env.local` 写入、幂等跳过）。
- E3 单测：构造小尺寸假 `model.bin` 验证检测/自愈路径；加载失败退出码 `EXIT_MISSING_DEP(3)`。
- E4 单测：解析顺序（显式 `VT_CUDA_DIR` → venv-torch 自动探测 → 系统 `CUDA_PATH`）；
  且**无关目录（不含 CUDA DLL）必须被拒绝**，系统 `CUDA_PATH` 不得抢在 venv-torch 之前。
- 对齐（T4）：`video-translate setup --align` 幂等可重复执行；`doctor` 显示
  `whisperx: OK`；**在没有 `NLTK_DATA` 环境变量时**对齐仍能自动定位项目内语料
  （不出现 LookupError —— 出现即意味着语料缺失导致对齐静默失效）。
