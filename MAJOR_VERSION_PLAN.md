# V5 开发计划：CUDA / Windows 部署 + GPU 衍生功能

> 状态：规划稿（未开工）
> 创建：2026-07-31
> 目标分支：`feat/v5-cuda-windows`（在 Windows 盒上从 GitHub 拉此分支开发）
> 版本号：以分支创建时的 `pyproject.toml` 为准（当前 `4.0.0`，下一版建议升 `5.0.0`）

---

## 0. 背景与铁律

### 0.1 现状快照（已核对代码）
- `pyproject.toml`：`version=4.0.0`，依赖 `faster-whisper==1.2.1` + `deep-translator==1.9.1`，`requires-python=">=3.10"`。
- 转写设备写死为常量：`src/video_translate/transcribe.py:25-26`
  ```python
  DEVICE = "cpu"
  COMPUTE_TYPE = "int8"
  ```
  在 `transcribe.py:171` 被 `WhisperModel(model_name, device=DEVICE, compute_type=COMPUTE_TYPE, cpu_threads=threads)` 使用。
- 翻译引擎：`src/video_translate/translate.py:50` `_make_translator`（Google），`translate_segments(translate_fn=...)` 已支持注入翻译器。
- CLI 引擎分支：`cli.py:229`(agent) / `:250`(google) 与 `:323` / `:336`（run 子命令）；`--engine choices=["agent","google"]`（cli.py:482 / :537）。
- 配置：`config.py:46` `engine="agent"`，env 映射见 `config.py:116-124`（含 `VT_ENGINE`）；**无 device / compute_type 字段**。

### 0.2 目标
在 **Windows（8GB RTX 3070 Ti）** 机器上：
1. 转写支持 CUDA（`device=cuda` + `int8_float16`），显著加速；
2. 新增 **本地 LLM 翻译**（Ollama + Qwen2.5），脱离云端 API、全离线；
3. 引入 **WhisperX 强制对齐**，根治 V4/V6 反复的时间戳漂移；
4. 叠加 **说话人分离**（pyannote，随 WhisperX）；
5. 提供 **批量 + Web UI**，GPU 盒常驻为服务，Mac 远程提交任务。

### 0.3 铁律（不可违反）
- **Mac 本地版本零改动、继续可用**。所有新功能都是增量 + 默认不变。
- 新开关默认值保持现状：`device=auto`（Mac 回退 cpu/int8）、`engine` 默认仍 `agent`。
- 不升级 Mac 路径的 `faster-whisper`（守住已调好的 VAD / 确定性 golden 回归）。
- 计划文档随仓库走，本文件即交付物。

> 依据：ADR-001 已明确"Revisit when: running on a dedicated NVIDIA box. A future ADR could add an opt-in device=cuda path guarded by nvidia-smi."——本计划正是该条款的落地。

---

## 1. 环境与版本隔离策略（最优先，决定成败）

| 维度 | Mac（保持不变） | Windows 构建盒 |
|---|---|---|
| Python | 3.13（当前可用） | **必须 3.12**（ctranslate2 无 3.13 wheel，GitHub issue #1240） |
| faster-whisper | 锁 `1.2.1` | 核心同 `1.2.1`；WhisperX 作为 Windows-only extra（自带更新版） |
| CUDA | 无 | CUDA 12.x + cuDNN 9（ctranslate2 最新版要求） |
| 新依赖 | 不装 | 仅 `[windows]` extra：`whisperx`、`ollama`(client)、`fastapi`、`uvicorn` |

- **依赖分组**：在 `pyproject.toml` 的 `[project.optional-dependencies]` 下新增
  ```toml
  windows = ["whisperx", "ollama", "fastapi", "uvicorn"]
  ```
  Mac 安装仍是 `pip install -e .`，Windows 用 `pip install -e ".[windows]"`。
- **显存纪律（8GB）**：转写用 `int8_float16`（**不**用 `float16`，防 OOM）；翻译步骤 Ollama 仅在转写释放显存后加载，单步峰值可控。
- **CUDA runtime on Windows**：PyInstaller 不会自动带 CUDA DLL。打包时附带 Purfview `whisper-standalone-win` 的 NVIDIA 库合集（cuBLAS/cuDNN 12）到 exe 同目录并加入 PATH；或文档要求用户装 CUDA Toolkit 12.x。
- **坑位**：`nvidia-cudnn-cu12` 的 9.x 某些版本与 faster-whisper 冲突（libcudnn 报错），需对齐 cuDNN 版本。

---

## 2. 任务拆解（按优先级）

### T1 — CUDA 设备抽象（核心，本版必做）
**改动点（均为新增/可选，不改 Mac 默认行为）：**
1. `transcribe.py:25-26`：删掉写死常量，改为从参数/配置读取。
   - `transcribe_video(..., device: str = "auto", compute_type: str = "auto")` 签名新增两参。
   - `:171` 改为 `WhisperModel(model_name, device=dev, compute_type=ct, cpu_threads=threads)`。
2. `config.py`：`Config` 新增字段 `device: str = "auto"`、`compute_type: str = "auto"`；env 映射加 `"device": "VT_DEVICE"`、`"compute_type": "VT_COMPUTE_TYPE"`（接在 `config.py:124` 附近）。
3. **auto 解析逻辑**（新增工具函数，如 `resolve_device(device, compute_type)`）：
   - `device="auto"` → 探测 `torch.cuda.is_available()`（或 `shutil.which("nvidia-smi")`）存在则 `cuda`，否则 `cpu`；
   - `compute_type="auto"` → cuda 时用 `int8_float16`（8GB 防 OOM），cpu 时用 `int8`；
   - Mac 上探测到无 CUDA → 仍走 `cpu/int8`，**输出与现状一致**。
4. **缓存键必须含 device+compute_type**：`transcribe_fingerprint(...)`（`transcribe.py:156`）追加 device/compute 入参，否则 cpu 产物会被 cuda 复用、反之亦然。
5. CLI：`transcribe` 与 `run` 子命令加
   ```python
   p.add_argument("--device", default="auto", choices=["auto","cpu","cuda"])
   p.add_argument("--compute-type", default="auto", choices=["auto","float16","int8","int8_float16"])
   ```
   并把值透传给 `transcribe_video` / `resolve_config`。
6. **回测（关键）**：Mac 上跑现有 golden 回归，确认 `device=auto` 在 Mac 上等价于原 `cpu/int8`，产物字节级一致（守住 ADR-001 确定性）。

### T2 — 本地 LLM 翻译（Ollama + Qwen2.5-7B INT4）
**改动点（复用现有注入式翻译器接口，改动极小）：**
1. `translate.py` 新增 `_make_ollama_translator(src, tgt, model="qwen2.5:7b-instruct-q4_0", base_url="http://localhost:11434") -> translate_one`：
   - `translate_one(text)` POST `http://localhost:11434/api/generate`，body 含 `model` + `prompt`；
   - `prompt` 复用现有 `TRANSLATION_GUIDELINES`（`translate.py:39`）+ `persona`，保持与 agent 引擎相同的翻译契约。
2. `translate_segments(..., translate_fn=...)` 已支持注入，ollama 走与 google 同一条 headless 路径。
3. `config.py`：`engine` 可选值加 `"ollama"`；新增 `ollama_model`、`ollama_base_url` 字段 + env（`VT_OLLAMA_MODEL` / `VT_OLLAMA_URL`）。
4. `cli.py`：`--engine choices` 加 `"ollama"`；`cmd_translate`（:229/:250 之间）与 `cmd_run`（:323/:336 之间）加 ollama 分支，行为同 google 分支（直接写 `zh_segments.json`）。
5. **Windows 部署文档**：装 Ollama → `ollama pull qwen2.5:7b-instruct-q4_0`（INT4 ~5GB 显存）。
6. 显存：转写结束释放后加载 7B，单步峰值可控。

### T3 — WhisperX 强制对齐（修 V4/V6 漂移）
**背景**：ADR-008 曾在 3.13 spike `stable-ts` 失败→走自写路线 A。Windows 用 3.12 可装 WhisperX，且 WhisperX 自带对齐（wav2vec2 词级强制对齐，精度 96% vs faster-whisper 82%）。
**改动点（Windows-only extra，Mac 不装）：**
1. `transcribe.py` 增加可选对齐钩子 `align_segments(segments, audio, language, align_backend="whisperx")`（仅当 `--align whisperx` 且库可用时调用），在 chunk 转写后、`merge.py` 处理前回写词级时间戳。
2. CLI：`transcribe`/`run` 加 `--align {none,whisperx}`（默认 `none`，Mac 行为不变）。
3. **不变量**：对齐只润词级/显示时间戳，不改段落语义与顺序（沿用 V4 时间戳不变量）。
4. **依赖隔离**：whisperx 仅在 `[windows]` extra 安装；若其自带 faster-whisper 与 `1.2.1` 冲突，则用 Windows 专属 venv，绝不影响 Mac。
5. **回退预案**：若 WhisperX 与 1.2.1 不兼容，改用 `stable-ts`（3.12 可装，且对齐质量优于裸 faster-whisper），同样走 Windows-only extra。

### T4 — 说话人分离（pyannote，随 WhisperX）
1. WhisperX `DiarizationPipeline`（`use_auth_token=os.environ["HF_TOKEN"]`），需 HF token。
2. 在 `merge.py` / `generate.py` 阶段给 cue 文本加 `Speaker N:` 前缀（可选 `--diarize`）。
3. 8GB 显存顺序执行（转写→对齐→分离→翻译），无叠加压力。

### T5 — 批量 + Web UI（GPU 盒常驻服务）
1. `FastAPI` 服务（`src/video_translate/server.py` 或 `tools/` 下新模块）：
   - `POST /jobs`：入参 `{video, options}`（options 复用现有 CLI 参数）→ 后台跑 `cmd_run` 流水线；
   - `GET /jobs/{id}`：查状态/产物路径；
   - 产物落盘到共享目录，Mac 通过 http 或共享盘取回。
2. 前端极简：文件夹批量提交 + 进度轮询（可先纯 HTML/JS，不引重框架）。
3. 部署：`uvicorn` 常驻；**Web 层只是 `cmd_run` 的包装**，核心逻辑零重复。
4. 与 T2/T3 协同：服务端默认 `device=cuda`、`engine=ollama`、`align=whisperx`，全离线跑通整条管线。

---

## 3. 打包与 Windows 部署

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

## 4. 风险与回退

| 风险 | 触发条件 | 对策 |
|---|---|---|
| faster-whisper 升级改 VAD | 引入 WhisperX 自带更新版 | Mac 锁 `1.2.1`；WhisperX 仅 Windows extra；升级后回测 B2 类误杀 |
| 8GB OOM | `float16` + 大模型同驻 | 转写用 `int8_float16`；转写/翻译/对齐**分步**执行；必要时降模型 |
| WhisperX 与 1.2.1 冲突 | 依赖不兼容 | 回退 `stable-ts`（3.12 可装）或仅对齐不换核心（T3 回退预案） |
| ctranslate2 无 3.13 wheel | Windows 误用 3.13 | 构建 venv 强制 3.12（T1 / §1） |
| 中文路径 / 文件锁 | Windows 文件系统差异 | T5 部署前专项测试 |
| cuDNN 9 冲突 | `nvidia-cudnn-cu12` 9.x 异常 | 对齐 cuDNN 版本；必要时钉 `ctranslate2` 版本（见 ADR 矩阵） |

---

## 5. 验收标准（每任务）

- **T1**：Mac golden 回归通过；Windows `nvidia-smi` 下 `device=cuda` 生效、速度提升；cpu/int8 产物与历史一致。
- **T2**：`--engine ollama` 离线产出 `zh_segments.json`，质量 ≥ google。
- **T3**：鲍德温类漂移样本时间戳误差 < 150ms。
- **T4**：多人视频 cue 带 `Speaker N:` 标签。
- **T5**：Web 提交 → 产出全流程跑通，Mac 可远程取回。

---

## 6. 分支与文档协同

- GitHub 开 `feat/v5-cuda-windows`；Mac 主分支保持可用、不合并本计划代码直至 Windows 验证通过。
- 新增 ADR（建议）：
  - **ADR-011**：撤销 ADR-001 的 CUDA 禁令，加 opt-in `device=cuda` 路径（呼应 ADR-001 的 "Revisit when"）。
  - **ADR-012**：修订「时间戳是声学事实」不变量，引入独立声学参照 + 分层校验（见正文，已落地）。
  - **ADR-013**：WhisperX 强制对齐（GPU 盒）引入决策，正式回应 ADR-008 的回退；Mac 永不引入（见正文，已落地）。
- 本计划文档（`MAJOR_VERSION_PLAN.md`）随仓库走，作为 Windows 端开发的唯一事实来源。

---

## 7. 实施顺序建议（在 Windows 盒上）

1. 搭 3.12 venv + `[windows]` extra，确认 `faster-whisper` CUDA 跑通（最小验证脚本）。
2. **T1** CUDA 抽象 + 回测 golden（最关键，先打通再叠加）。
3. **T2** Ollama 翻译（独立于对齐，先享离线红利）。
4. **T3 → T4** WhisperX 对齐 + 分离（修漂移、加说话人）。
5. **T5** Web UI 收尾（把前面能力暴露成服务）。
