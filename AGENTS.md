# AGENTS.md — AI Agent Execution Protocol

You are an AI agent asked to turn a video into bilingual (zh/en) subtitles using
this project. Follow this protocol. It is tool-agnostic (WorkBuddy, Claude Code,
Cursor, Cline, plain shell). **Do not reinvent the pipeline** — the rules below
encode resume-safety, proxy correctness, the control-plane state machine (flow
progression lives in code, [ADR-030](docs/adr/030-control-plane.md)), the
agent-as-engine translation step, and strict three-lane verification. Your role
is **Agent-as-Translator** (翻译 + 语义回读)，流程推进与闸门由代码状态机负责。

Read order: this file → [`TOOLCHAIN.md`](TOOLCHAIN.md) for environment setup →
[`docs/TOOLING.md`](docs/TOOLING.md) for tool/dependency management (E1–E4) →
[`docs/specs/00-overview.md`](docs/specs/00-overview.md) for behavior →
[`docs/adr/030-control-plane.md`](docs/adr/030-control-plane.md) for the state
machine (exit codes / `status` / NEXT blocks) → [`docs/adr/`](docs/adr) for the
full architectural rationale.

---

## 1. 避坑防呆红线速查 (Anti-Patterns & Traps Guard)

> **Agent 必读**：以下是历史迭代中高频出现的严重错误。严禁违反任何一条！

| 陷阱类别 | ❌ 严禁的错误操作 (Anti-Pattern) | 💥 致命后果 (Consequence) | ✅ 唯一正确做法 (Correct Pattern) |
|---|---|---|---|
| **声学时间戳** | 在断句合并、翻译或后处理中篡改/重算 `start`/`end` | 破坏声学对齐，字幕与实际人声发音错位 | 严格保留转写产生的时间戳；下游**只改文本，绝不重算时间轴**。 |
| **强制对齐（T4）** | 误以为 `--align whisperx` 会改写文本/断句、或把对齐接到 Mac / 默认安装；更动 `transcribe_fingerprint` 让切换 `--align` 触发重转写 | 文本/断句漂移、Mac 装不上 whisperx 崩溃、golden 回归失败、GPU 重转写浪费 | 对齐 pass 本身只改 `words[].start/end`（及由词推导段边界），对齐输入输出的段数/文本/分组不变；**默认 `auto`（T4 默认化）**：CUDA + whisperx 可用即走 whisperx，Mac/未装自动降级 `none` 不崩溃（行为零变化）；显式 `--align none` 仍是历史字节级路径（golden 保护）；whisperx 仅在 `[gpu]` extra（Windows/Linux+CUDA）；对齐是**独立缓存层**，不进转写指纹（切 `--align` 不重转写）。**但下游 merge 会按更准的词间气口重新断句，最终段数可能变**（实测 40→42），见下一条。详见 [ADR-028](docs/adr/028-whisperx-alignment-pass.md) / [Spec 22](docs/specs/22-whisperx-alignment.md)。 |
| **对齐后翻译失效（T4）** | 对齐（默认 `auto`：GPU 机器转写即走 whisperx）后不重译，直接拿旧的 `zh_segments.json` 跑 `generate` | 对齐把段边界收紧到真实词边界（去掉首尾静音），且更准的气口让 merge 改变断句 → **段数变化**，旧翻译按 index 对不上，中英错行串行（实测 40 段字幕对 42 段英文） | **只要走了对齐（GPU 机器默认即对齐），转写完成后就必须重新翻译**（至少补齐新增 index）再 `generate`；`generate` 的 `verify_align` 与 `verify` 内容 lane 会报行数/覆盖不匹配，不要绕过它。重跑 OCR 之外的一切都可复用，唯独翻译要重做。 |
| **断点缓存** | 遇到报错或重试时执行 `rm chunk_*.json` 或删除中间缓存 | 摧毁断点续跑机制，长视频被迫全部从头重跑 | 保留所有分块缓存；若仅需重跑翻译与生成，使用 `run --skip transcribe`。 |
| **表现层窗口** | 人为传入 `--tail 0 --min-dur 0` 试图“缩短/收紧”字幕 | 字幕在发音前一闪而过、或人未说完字幕已消失 | 保持默认值 `--tail 0.3 --min-dur 1.0`（阅读呼吸余量）；仅用 `--offset` 微调整体早出。 |
| **剪映缓存碰撞** | 重新生成字幕时 `rm -rf` 视频输出子目录 | 剪映内部缓存记住同名文件，导致新字幕在剪映内不生效 | 严禁删除输出目录；让 `generate` 自动递增版本号（如 `_v2`、`_v3`）。 |
| **翻译索引对齐** | 翻译时漏行、合并行或输出非数字 key 的字典 | 导致中英文索引错位（英文对齐正常，中文整批串行） | 严格输出 `{"<str(index)>": "<zh>", ...}`，**必须 100% 覆盖全部 index**。 |
| **漏音补洞 VAD** | 在 `fill_gaps` 漏音补洞流程中强制叠加 `--vad` | 笑声、欢呼或音乐垫底下的真实语音被 VAD 二次抹杀 | `fill_gaps` 恢复解码阶段**恒为裸跑（无 VAD）**，不透传全局 VAD。 |
| **人声分离时长** | 用 ffmpeg/demucs 手动裁切/重采样后再喂给 Whisper，或质疑「分离后时长 ≠ 原视频」为 BUG | 字幕时间戳全局漂移，1s 错位 = 全片报废 | 分离输出**必须** `|dur(out) - dur(orig)| < 50ms`；否则 CLI 自动降级回原音频，不要手改。 |
| **8GB GPU OOM** | 用脚本并行跑 demucs + Whisper，或在同一进程让两模型常驻显存 | RTX 3060/4060 级别必炸 CUDA OOM | CLI 已保证「demucs→释放显存→Whisper」顺序；若需脚本调用，也必须遵守「单一大模型串行」。 |
| **人声分离缓存** | 开了 `--separate-vocals` 又手动 `rm chunk_*.json` 试图「强制重跑 Whisper 但保留 vocals.wav」 | chunk 指纹已嵌入 vsep 参数，删缓存只删一半会让 resegment/fill_gaps 找不到同路径 vocals.wav | 正常跑不用管缓存；真要清空就把输出目录的 `<base>.vocals_*.wav` 和 `chunk_*.json` 一起删，或换个 base。 |
| **工具链查找** | 仅因 `where ffmpeg` 为空便向用户报错停摆 | 忽略了 `.env` 注入机制，造成虚假缺失报错 | 运行 `uv run video-translate doctor`，或按 [TOOLCHAIN.md](TOOLCHAIN.md) 配置 `.env` 中的 `VT_FFMPEG_DIR`。 |
| **环境定位（命令入口）** | 裸跑 `python` / `video-translate` / `make`，指望 PATH 指向项目环境 | 命中 PATH 里残留的旧全局环境（如 `F:\Python311`，缺 whisperx），`doctor` 误报未安装、反复排障 | **所有命令一律 `uv run ...`**（在项目根执行），`uv` 自动定位项目 `.venv`；开新窗口先 `cd <repo>` 再加 `uv run` 前缀。详见 [Spec 23](docs/specs/23-environment-location.md) / [ADR-029](docs/adr/029-command-entry-uv-run.md)。 |
| **Exit Code 6** | 遇到程序退出码 6 时当成错误反复重试 `run` | 死循环卡在转写步骤，无法进入翻译 | 退出码 6 是 `[AWAITING_AGENT]` 挂起信号，表明转写已完成，等待 Agent 执行翻译。 |
| **依赖装错环境** | 把 `demucs`/`torch` 这类重依赖放进 optional extra（`[audio]`）或只写在 `requirements.txt` 却不进 `pyproject` 顶层 `dependencies` | 默认 `pip install -e .` 不装它，依赖飘到系统 Python、venv 里 `import` 不到、`--separate-vocals` 静默降级 | **任何运行时依赖都写进 `pyproject` 顶层 `dependencies`**（不藏 extra）；装环境只用 `pip install -e .` / `uv sync` 一条命令，不依赖额外动作。详见 [TOOLCHAIN.md](TOOLCHAIN.md) §依赖与 wheel 镜像。 |
| **CUDA wheel 装成 CPU 版** | 用 `pip install`（不带 `--index-url`）装 `torch`/`torchaudio`，或以为 `[tool.uv.sources]` 对 pip 生效 | 无代理时 pip 回退 PyPI 默认 `+cpu` wheel，`torch.cuda.is_available()`=False，GPU 加速失效 | **CUDA 版必须走镜像索引**：`uv sync`（认 `[tool.uv.sources]`，自动按平台选 CUDA/CPU wheel，无需任何手工参数）或 `pip install torch --index-url https://download.pytorch.org/whl/cu128/`（CN 无代理可换清华 `https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cu128/`）。绝不裸 `pip install torch`，且索引版本必须与 `pyproject` 一致（当前 **cu128** / torch 2.8 线，由 `[gpu]` extra 的 whisperx 3.8.x 决定）。详见 [TOOLCHAIN.md](TOOLCHAIN.md) §依赖与 wheel 镜像。 |
| **镜像源靠 Agent 临选** | Agent/人工每次安装时现场拼 `--extra-index-url` 或挑代理 | 换人或换机就装不动、或装错源，不可复现 | **镜像源固化进 `pyproject` 的 `[tool.uv.index]`**（cu128 官方 PyTorch 索引，`explicit = true` 让它只服务 torch/torchaudio，不遮蔽 PyPI 上的通用包）+ `PIP_EXTRA_INDEX_URL` 进 [TOOLCHAIN.md](TOOLCHAIN.md)；安装一律程序决定，不靠临场决策。 |
| **尾部回音幻觉** | 在笑声/欢呼/掌声等"有能量无语义"窗口后，看到新段复述上一句尾部（如真句 `give me a yogurt either way.` 后冒出 `I'm not hungry either way.`）时，手工删段或重算时间戳 | 手工删段破坏 index 对齐、重算时间戳破坏声学层；且下次重跑又复现 | 这是 Whisper 自回归固有缺陷（ADR-020）。**不要手工改**，靠 `drop_hallucination_segments` 自动拦截：段内词与前驱**逐字共享时间戳且含零时长词**（第四信号）即判回音；转写层已携带 `avg_logprob` 供第五信号。两信号已在单测覆盖，全片重跑自动生效。 |
| **依赖与外部工具** | 加依赖只改 `pyproject` 不提交 `uv.lock`；手动下载 ffmpeg/模型塞进仓库或散落各盘 | 换机版本飘移、装成 CPU wheel、二进制垃圾散落缓存 | 一切依赖与外部资产按 [MAJOR_VERSION_PLAN.md](MAJOR_VERSION_PLAN.md) §3.2 规则 R1-R7 执行：依赖进顶层 + lockfile 成对提交；ffmpeg 由 `setup --ffmpeg` 自动下载；模型默认落项目根 `models/`（零 C 盘）不进 git。详见 [docs/TOOLING.md](docs/TOOLING.md)。 |
| **补洞恢复段幻觉** | `fill_gaps` 漏音补洞恢复出的 `_recovered` 段（如 `Don't worry.`/`I'm a clown.`/`Hi, son.`/`Now what?`/`This is bad.`）与邻居段**时间窗口重叠**（骑在已确认音频上）或**语速物理不可能**（3 词塞进 0.16s），却因只过了文本相似度检查而溜进字幕 | 转写层 `drop_hallucination_segments` 只作用于 Whisper 原产段、在 fill_gaps **之前**运行，补洞恢复段完全绕过了它；手工改会破坏断点续跑 | `fill_gaps` 已内置 `_is_recovered_hallucination` 守卫（ADR-020 补遗/ADR-021）：恢复段与现有段重叠 >0.12s、或语速 >8wps、或 `no_speech_prob`>=0.6（Whisper 自判非语音，最强信号，avg_logprob 不设闸）、或 `avg_logprob`<-1.0、或**零时长词 ≥2 个**（DTW 坍缩指纹，如开头非语音能量被硬拼成 `Hubsan x4 H502E...`/`We'll be right back.`/`Thank you.` 全部被拦）即丢弃；collapse 替换路径关闭重叠信号以免误杀真替换。**resegment 拼接产出同过此守卫**（ADR-031 D1，拦截打印可见不静默），保留段打 `origin: "resegment"`（D2，恢复段类别对 verify/回读可见）。已单测固化，**全片重跑自动生效，不要手工改**。 |
| **断句切点** | 看到句尾词被掐到下一条字幕（`my sister` ‖ `deidre`、`we don't` ‖ `know`）时，手工在剪映里挪词或改文本 | 破坏 index 对齐与声学时间戳；下次重跑复现 | 42 字符剪映上限必须切，但 V8 已让切点**智能回退**（ADR-022）：优先标点边界→次选 >0.3s 词间气口→贪心兜底；句首连接词孤儿（`because`）自动并右。无标点+零间隙的密集语流物理无解，等 whisperX 对齐后气口浮现。重跑自动生效。 |

---

## 2. 质量护栏体系 (Quality Guardrails)

字幕质量分解为 **声学层 / 内容层 / 表现层** 三个正交维度（[ADR-012](docs/adr/012-acoustic-timestamp-truth.md) / [Spec 18](docs/specs/18-verify.md)）：

| 维度 | 决策点 | 核心规则 | 依据 |
|---|---|---|---|
| **声学层**（时间轴压在真语音） | VAD 路由 | 依据 `doctor --video` 音频画像**自动路由**，禁止盲猜 | ADR-011 / ADR-012 |
| **声学层** | 漂移与漏检 | 对照 `silencedetect` 独立参照，探测静音跨越与 `uncovered-audio` (≥2s 无 cue 语音窗) | ADR-012 / ADR-016 |
| **声学层** | 幻觉拦截 | 转写层 `drop_hallucination_segments` 五信号：word 塌缩≥50%+邻居3-gram 重复 / 整段落静音窗 / **尾部回音（窗口被邻居时间窗包含且含零时长词，确定性）/ Whisper 低 `avg_logprob`**；**补洞恢复段**另有 `_is_recovered_hallucination` 守卫（重叠>0.12s / 语速>8wps / 低置信度）兜住绕回 fill_gaps 的回音；**resegment 拼接产出同过此守卫**（拦截打印可见）；verify 声学 lane 巡检**段级置信度**（`no_speech_prob`≥0.6 / `avg_logprob`<-1.0 → 红灯）与**相邻段重叠/词碰撞**（恢复段首词骑邻居词 = 幻觉前缀指纹，只报告不修剪） | ADR-012 / ADR-020 / ADR-021 / ADR-031 |
| **内容层**（zh 忠实于 en） | 覆盖与对齐 | `validate_zh`（覆盖率）→ `verify_align`（Pearson 索引对齐）→ 中英混杂词检测 → **语义回读（默认开启）** | Spec 17 / Spec 18 |
| **表现层**（出入字时机） | 显示窗口 | 保持 `tail 0.3 / min-dur 1.0` 默认值；防剪映缓存碰撞自动 `_vN` 递增 | Spec 04 / ADR-012 |

**VAD 路由决策表（由 `doctor` 自动给出）**：
- 干净单人录音 / 朗诵 → `--vad`（段边界钉在真实静音，根除漂移）
- 音乐重 / 低信噪比 / 耳语 → 裸跑（默认）+ `fill_gaps` 自动补洞
- 干净但电平偏低（`mean < -20` 或 `max < -5`）→ 先 `loudnorm` 归一化，再 `--vad --vad-threshold 0.1`
- **混合音频**（干净对话与欢呼/笑声/BGM 交替）→ `--adaptive-vad`（按 chunk 音频画像动态路由，[ADR-015](docs/adr/015-adaptive-per-chunk-vad.md)）
- **强 BGM / 原声带影片 / MV / 演唱会**（[Spec 19](docs/specs/19-vocal-separation.md) / [ADR-017](docs/adr/017-vocal-separation.md)）→ 先 `--separate-vocals` 跑 Demucs 提取人声轨，再按画像裸跑或 `--adaptive-vad`

---

## 3. 标准执行状态机 (Control-Plane Client Interface)

> **流程推进收归代码状态机**（[ADR-030](docs/adr/030-control-plane.md)）：阶段顺序、
> 闸门、修复指引全部由代码执行与输出。Agent 的职责 = **翻译（Phase 2）+ 语义回读**，
> 其余只做「按状态机的指示跑命令」。不知道该干什么时，问状态机，不要凭记忆编排：
> ```bash
> uv run video-translate status            # 你在哪 / 缺什么 / 下一步（人读）
> uv run video-translate status --json     # 机器可解析（agent 读这个）
> ```

### Phase 0: 环境就绪 (Preflight — 一次性协议)

> **环境必须一步到位，禁止自由发挥配环境。** 绝不允许 Agent 自行把 FFmpeg / 模型 / 工具链散落缓存到各处。所有环境就绪动作统一走确定入口（命令一律 `uv run`，恒定位项目 `.venv`，[Spec 23](docs/specs/23-environment-location.md)）：
> ```bash
> cd <repo>                       # 先进入项目根
> uv run video-translate setup    # 一键装齐：uv sync 依赖 + 预拉 large-v3 模型（约 3GB）
> uv run video-translate doctor   # 校验：FFmpeg/ffprobe 缺失默认 exit 7（硬依赖）；CUDA/模型等其余项 --strict 才拦，全绿才继续
> ```
> 未装 uv 时先按官方 installer 安装（Windows `irm https://astral.sh/uv/install.ps1 | iex`；macOS/Linux `curl -LsSf https://astral.sh/uv/install.sh | sh`），见 [TOOLCHAIN.md](TOOLCHAIN.md) §1.3。模型 `[MISS]` → 重跑 `setup` 预拉；FFmpeg / ffprobe `[MISS]` → `uv run video-translate setup --ffmpeg` 自动下载便携版。**不要**手动到处下载、改 `.env` 假设模型配置、或全盘搜（E2 已消灭「全盘搜」）。

1. **定位视频**：优先查找 `videos/` 目录；若为空或多文件，与用户确认目标视频。
2. **跑 `doctor`（含 `--video`）看环境与音频画像**：doctor 给出的 VAD / 人声分离建议
   **不再由 agent 重演决策**——`run` 时显式传参（以 origin=explicit 落盘）或直接用默认
   （自动路由，落盘 origin=default）。全部决策与默认值记录在 `<base>.vt_state.json` 的
   `decisions` 字段，随时可查；显式选择缺能力时意图闸会硬停（exit 8），见 §3.5。

---

### Phase 1: 转写（状态机推进）
```bash
# 标准运行（doctor 建议自动路由；显式 flag = origin explicit 落盘）
uv run video-translate run "videos/<video.mp4>"

# 强 BGM / 伴奏 / 噪音场景（doctor 推荐 RECOMMENDED 时）
uv run video-translate run "videos/<video.mp4>" --separate-vocals
```
- 分块可续跑设计（`chunk_N.json` 自动断点恢复）；**显式 `--separate-vocals` 但 demucs 未装 = exit 8 硬停**（不再静默回退原音频），按指引 `uv sync` 后重跑。
- **强制声学对齐（T4，默认 `auto`）**：CUDA + whisperx 可用即走 WhisperX 词级时间戳精修（独立缓存层，不重转写）；Mac/未装自动降级 `none`（降级原因落盘 `decisions.align.resolved`）。**显式 `--align whisperx` 而包不可用 = exit 8**；`--align none` 仍是历史字节级路径（golden 保护）。**对齐后段数可能变（实测 40→42）→ 转写完成后必须重译**（generate 闸门会拦陈旧翻译）。
- 转写 + 断句合并 + 漏音补洞完成后：生成 `<base>.translate_task.json`，状态链推进至 `translate` 停点，尾部打印 **[NEXT] 块**（`--json` 机器可解析），返回 **Exit Code 6 (`[AWAITING_AGENT]`)** —— 这是正常停点，不是错误，勿重试。
- **翻译风格（T3 / ADR-027）**：`--style {film,literal,bilingual_study}` 选择翻译人设与守则（默认 `film` 影视二创口语感；`literal` 忠实直译；`bilingual_study` 双语精读）。多风格 `--style film,literal` 一次生成多份任务文件（`<base>.film.translate_task.json` 等）。显式 `--persona`/`VT_PERSONA` 覆盖风格预设人设。

---

### Phase 2: Agent 翻译 (Agent-as-Engine)
作为翻译引擎，Agent 执行以下步骤：
1. 读取 `<base>.translate_task.json`（或多风格下的 `<base>.<style>.translate_task.json`），
   阅读 `full_transcript` 全局上下文、`source` 背景提示与 `persona` / `guidelines` 设定。
2. 逐批翻译 `to_translate` 中的每一项（遵循 `persona` 与 `guidelines` 指定的风格取向，
   默认「信达雅 + 口语感」，保留语气情绪）。
3. 生成 `<base>.zh_segments.json`（多风格下为 `<base>.<style>.zh_segments.json`），
   格式为严格的 `{"<str(index)>": "<zh>", ...}` 字典，**必须 100% 覆盖所有 index**。
4. （可选）校验覆盖完整性：
   ```bash
   uv run python -c "from video_translate.translate import validate_zh; print(validate_zh('<base>.segments_en.json', '<base>.zh_segments.json'))"
   ```

---

### Phase 3: 字幕生成（enforce 闸门内置）
```bash
uv run video-translate generate \
    --segments "videos/<base>.segments_en.json" \
    --zh "videos/<base>.zh_segments.json" \
    --outdir "videos" --base "<base>"
# 多风格时，对每个风格分别生成（文件名带 .<style> 后缀）：
uv run video-translate generate \
    --segments "videos/<base>.segments_en.json" \
    --zh "videos/<base>.literal.zh_segments.json" \
    --outdir "videos" --base "<base>" --style literal
```
- **前置闸门（exit 8 硬停）**：zh 覆盖率 < 100%、en/zh 段数不匹配、`verify_align` 索引漂移、
  状态链 `segments_sha` 陈旧（对齐后忘重译）→ 一律**拒绝生成**并给出修复指引。这是 40→42 段
  错行事故的机器防线，不要绕过；确需降级才显式传逃生门 `--allow-degrade`。
- 输出 4 个核心产物（`.bilingual.srt`、`.zh.srt`、`.en.srt`、`.txt`）；落地于独立的 `<base>/`
  子目录，自动 `_vN` 版本递增以规避剪映导入缓存；`--style <name>` 让输出文件名带
  `.{style}` 后缀。成功后尾部同样打印 [NEXT] 块（下一步 = verify）。

---

### Phase 4: 门禁自检与交付 (Verify — strict 默认)
```bash
uv run video-translate verify \
    --segments "videos/<base>.segments_en.json" \
    --zh "videos/<base>.zh_segments.json" \
    --video "videos/<video.mp4>"
```
- `--zh` / `--video` **必填**（缺失 = exit 2 拒跑）：verify 必须跑全 lane，局部自检不允许冒充通过。
- **strict 默认**：任一 lane 红灯 = **exit 8**；报告模式才用 `--no-strict` 显式逃生。
- **verify 重试限制（ADR-031 D8）**：同一次翻译任务中，`verify` 第 1–2 次为严格门（红灯
  **exit 8**）；第 3 次及以后**强制降级报告模式**（打印问题列表、exit 0，不再闸门拦截），
  防止 Agent 陷入「修 fix 的 fix」的本地振荡。计数器 `stages.verify.attempts` 存于
  `vt_state.json`：`run`（完整流水线）重置为 0；`generate`/`resegment`（局部重跑）**不重置**
  ——看到 `[verify] retry limit reached` 即表示重试上限已到，请**人工审阅**问题列表决定
  后续修复（新增窗口、换翻译风格、重跑完整 `run` 等），不要无限重跑 verify。
1. **声学 Lane**：对照 `silencedetect` 检查静音重叠与漏检 (`uncovered-audio`)；**画像失败 / 探测异常 = 红灯**（不再 skip 或吞异常）；**段级置信度巡检**（`no_speech_prob`≥0.6 / `avg_logprob`<-1.0 = 红灯，ADR-031 D3）与**相邻段重叠/词碰撞巡检**（恢复段前缀骑邻居音频 = 红灯 hint，D4/D5）；uncovered 窗自动对照 vocals.wav 能量分级（`[bgm]`/`[speech]`/`[ambiguous]` 建议行，D7——resegment 选窗不再靠 Agent 临场 volumedetect）；报告打印恢复段清单（高嫌疑提示，D2）。
2. **内容 Lane**：行数覆盖、索引漂移、未翻译英文残留，并生成 `<base>.semantic_reread_task.json` 供 Agent 结合邻居语境快速回读标记。
3. **表现 Lane**：检查显示窗口参数完整性。
4. **语义回读闭环**：verify 会消费 `<base>.semantic_reread_result.json`——存在且含非 ok 判定 = 红灯；**缺失则重挂 task 并置状态 `pending_agent`**（已产出的 SRT 不撤回，但状态链不显示「完成」）。
5. **交付**：全绿后向用户汇报最终字幕路径，剪映导入主文件为 `<base>.bilingual.srt`。

---

### 3.5 状态机速查 (Control-Plane Cheat Sheet)

**退出码（0–8）**：

| 码 | 常量 | 含义 | Agent 该做什么 |
|---|---|---|---|
| 0 | `EXIT_OK` | 成功 | 按 [NEXT] 块走下一步 |
| 1 | `EXIT_RUNTIME` | 运行时错误 | 读 stderr；用 `status` 查进度；**勿删缓存** |
| 2 | `EXIT_ARGS` | 参数错误（如 verify 缺 `--zh`/`--video`） | 补齐参数重跑 |
| 3 | `EXIT_MISSING_DEP` | 依赖缺失（模型残缺等） | 跟指引 `uv run video-translate setup` |
| 4 | `EXIT_PROXY` | 代理不可用 | 查代理配置（[TOOLCHAIN.md](TOOLCHAIN.md)） |
| 5 | `EXIT_KILLED` | 进程被杀（OOM/手动终止） | 断点缓存仍在，直接重跑 |
| 6 | `EXIT_AWAITING_AGENT` | **停点 A**：转写完成，等翻译 | 进 Phase 2 翻译；**正常停点，勿重试 run** |
| 7 | `EXIT_DOCTOR_FAIL` | doctor 自检不过（**默认 ffmpeg/ffprobe 缺失即触发**；`--strict` 下其余依赖项也拦） | 按 doctor 输出修环境（ffmpeg 缺失 → `setup --ffmpeg`） |
| 8 | `EXIT_GATE_FAIL` | **闸门拦截**（意图闸 / generate 前置 / verify strict） | 读修复指引；确需降级才显式传逃生门 |

**`status --json` 示例**（字段稳定，机器可解析）：

```json
{
  "base": "demo",
  "current_stage": "translate",
  "done": false,
  "pending_agent": true,
  "next_action": {
    "stage": "translate",
    "stop_point": true,
    "cli": "(agent) 翻译 <base>.translate_task.json -> <base>.zh_segments.json (100% 覆盖全部 index)",
    "blocked_by": []
  },
  "artifacts": { "segments": "videos/demo.segments_en.json", "zh": null, "srt": null },
  "verify_status": null
}
```

**[NEXT] 块**：`run` / `generate` 尾部输出 `[NEXT] stage=<id>` + `do: <命令>`，是状态机给出的
**唯一下一步**；`[NEXT] stage=translate (STOP POINT — awaiting agent)` 即停点 A。

**`pending_agent` 处理**：语义回读未完成。Agent 读 `<base>.semantic_reread_task.json`，逐对
(en, zh) 标记 `ok / omit / add / wrong / untranslated`，写入
`<base>.semantic_reread_result.json`（`{"<index>": "<verdict>: <reason>", ...}`），再跑
`verify` 即消费并解除 pending。**不回写 SRT、不改时间戳。**

**`<base>.vt_state.json`**：状态链落盘（`decisions` 决策含 origin、`stages` 各阶段 status +
segments_sha 指纹、capabilities 快照）。**只读参考，禁止手改**；损坏会被自动从产物文件重建，
删除亦无害——闸门只依赖产物文件本身（segments/zh），不依赖 state（防绕过原则）。

---

## 4. 辅助分支 (Auxiliary Workflows)

### 4.1 全自动无头模式 (`--engine google`)
若无需 Agent 介入的高质量翻译：
```bash
uv run video-translate run "videos/<video.mp4>" --engine google
```
翻译失败项将沉淀至 `<base>.agent_pending.json`。

### 4.2 补录回填 (`backfill`)
针对 Google 引擎未译出的段落：
```bash
# 1. 生成待补任务
uv run video-translate backfill --pending "<base>.agent_pending.json" --out "<base>.zh_segments.json"
# 2. Agent 翻译后保存为 your_zh.json
# 3. 回填并重新生成
uv run video-translate backfill --pending "<base>.agent_pending.json" --out "<base>.zh_segments.json" \
    --agent-zh your_zh.json --segments "<base>.segments_en.json" --outdir "videos/<base>" --base "<base>"
```

### 4.3 局部多语种重转写 (`resegment`)
针对特定时间窗口的混杂语种修正（如预告片中夹杂的日语片段）：
```bash
uv run video-translate resegment --segments "<base>.segments_en.json" --video "<video.mp4>" \
    --windows 12.0-18.5 41.0-45.0 --lang ja
```

### 4.4 P0→P1 修复批准门 (ADR-031 D8)
> 修质量问题（幻觉漏网、闸门误伤、残窗不可收）时，修复过程分两阶段推进，中间
> **必须经过人工批准点**，禁止一口气连续改到「修好为止」：

1. **P0 — 守卫补丁**：先用守卫/巡检补丁堵住复发路径（参照「修产物已错的两条正路」），
   并跑通回归测试。完成后**打印变更摘要**（改了哪个守卫、新增哪个测试、用什么事故几何
   数据复现、为何当初漏过），并给出 ADR / 测试文件引用。
2. **批准停点**：打印摘要后**等待最多 1 分钟**人工批准。用户可提前确认（OK）继续；
   超时自动放行进入 P1。若被否决，**停止**并等待新指示，不要自行继续优化。
3. **P1 — 优化**：仅在批准（或超时放行）后进行，如调阈值、新增巡检、补文档。

> 纯执行约束，零代码变更（由 AGENTS.md 协议而非状态机强制）；与 verify 重试限制
> （§3 Phase 4）互为表里：P0→P1 管「怎么修」，retry limit 管「修几轮」。

### 4.5 Agent 决策点协议（翻译流水线 P0→P1，ADR-032）

> **一句话使用方式**：用户只说「翻译 XXX 视频」，全程不敲命令。Agent 自动完成 P0 画像后，
> 在聊天里停下来，用**引导选择式**把三个决策项（含推荐默认值）摆给用户，默认等待
> **5 分钟**（`VT_DECISION_TIMEOUT_SECONDS`，可在 `.env` / `.video-translate.toml` 改），
> 用户回复按其选择执行，**超时未回复则按画像推荐自动执行**，随后全自动完成转写→翻译→生成→校验→交付。

**决策点三个项（含推荐默认）**：
| 决策项 | flag | 推荐默认来自 | 说明 |
|---|---|---|---|
| 翻译风格 | `--style` | `config.style`（默认 `film`） | film / literal / bilingual_study；画像无依据，仅用户偏好 |
| VAD 策略 | `--vad` / `--adaptive-vad` | `profile_recommendation()` | 干净录音开 `--vad`；连续噪声走 `--adaptive-vad` 分块路由 |
| 人声分离 | `--separate-vocals` | `profile_recommendation()` | 强 BGM / 伴奏 / 噪音时开（需 demucs） |

**协议步骤**（Agent 在聊天里执行；CLI 无法感知聊天，故 5 分钟是 Agent 等待行为而非 CLI sleep）：
1. 跑 `uv run video-translate doctor --video <video>` 完成环境体检 + 音频画像，读其
   `recommendation:` 输出（风格 / VAD / 分离 / 阈值 + rationale）。该推荐来自
   `audio_profile.profile_recommendation()`，会落盘 `decisions.audio_profile`。
2. 在聊天里输出引导选择式提问，列出三项 + 推荐默认，并说明等待 `VT_DECISION_TIMEOUT_SECONDS`（默认 300s）。
3. 用户回复 → 按其选择构造 `run` 命令（`--style/--vad/--adaptive-vad/--separate-vocals` 显式传参，
   以 `origin=explicit` 落盘）；超时未回复 → 不带这些 flag 直接 `run`，由 `cmd_run` 按画像推荐自动路由（origin=profile）。
4. 跑 `uv run video-translate run "<video>"`（无快照时 `cmd_run` 自动补画像并落盘，**绝不裸跑无画像**；
   三决策按 `CLI flag > routing > 画像推荐` 合并）。

> **兜底**：即便 Agent 失守直接 `run`，`cmd_run` 也会自动画像 + 自动路由（origin=profile），不会跳过 P0。
> 若要强制「必须人工决策」，加 `--require-profile` 硬闸：无 `origin=explicit` 的 routing 时 `run` 直接 exit 8。

**配置位置（用户自行修改）**：
- 决策点超时：`VT_DECISION_TIMEOUT_SECONDS`（默认 300） → `.env` 或 `.video-translate.toml` 的 `decision_timeout_seconds`
- 默认风格 / VAD 阈值等：对应 `VT_STYLE` / `VT_VAD_THRESHOLD`（默认 0.35）等，见
  [config.py](../src/video_translate/config.py) 与 `.env.example`

---

## 5. 跨工具执行备忘

- **断点续跑原则**：严禁在排查问题时删除 `chunk_*.json` 或 `segments_raw.json`。若只需重跑翻译与生成，使用 `run --skip transcribe`。
- **环境隔离详情**：参见 [`TOOLCHAIN.md`](TOOLCHAIN.md)。
- **控制平面详情**：状态机 / 闸门 / 退出码参见 [ADR-030](docs/adr/030-control-plane.md) 与 [`docs/CONTROL-PLANE-PLAN.md`](docs/CONTROL-PLANE-PLAN.md)；`Makefile` 入口已移除（与红线 R7 一致：一切命令 `uv run` 前缀，编排不设第二软约束）。
- **历史演进与技术案例**：参见 [`docs/HISTORY.md`](docs/HISTORY.md) 与 [`docs/POSTMORTEM-JamieFoxx.md`](docs/POSTMORTEM-JamieFoxx.md)。
