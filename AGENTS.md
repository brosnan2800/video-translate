# AGENTS.md — AI Agent Execution Protocol

You are an AI agent asked to turn a video into bilingual (zh/en) subtitles using
this project. Follow this protocol. It is tool-agnostic (WorkBuddy, Claude Code,
Cursor, Cline, plain shell). **Do not reinvent the pipeline** — the rules below
encode resume-safety, proxy correctness, the agent-as-engine translation step,
and three-lane output verification.

Read order: this file → [`TOOLCHAIN.md`](TOOLCHAIN.md) for environment setup →
[`docs/specs/00-overview.md`](docs/specs/00-overview.md) for behavior →
[`docs/adr/`](docs/adr) for architectural rationale.

---

## 1. 避坑防呆红线速查 (Anti-Patterns & Traps Guard)

> **Agent 必读**：以下是历史迭代中高频出现的严重错误。严禁违反任何一条！

| 陷阱类别 | ❌ 严禁的错误操作 (Anti-Pattern) | 💥 致命后果 (Consequence) | ✅ 唯一正确做法 (Correct Pattern) |
|---|---|---|---|
| **声学时间戳** | 在断句合并、翻译或后处理中篡改/重算 `start`/`end` | 破坏声学对齐，字幕与实际人声发音错位 | 严格保留转写产生的时间戳；下游**只改文本，绝不重算时间轴**。 |
| **断点缓存** | 遇到报错或重试时执行 `rm chunk_*.json` 或删除中间缓存 | 摧毁断点续跑机制，长视频被迫全部从头重跑 | 保留所有分块缓存；若仅需重跑翻译与生成，使用 `run --skip transcribe`。 |
| **表现层窗口** | 人为传入 `--tail 0 --min-dur 0` 试图“缩短/收紧”字幕 | 字幕在发音前一闪而过、或人未说完字幕已消失 | 保持默认值 `--tail 0.3 --min-dur 1.0`（阅读呼吸余量）；仅用 `--offset` 微调整体早出。 |
| **剪映缓存碰撞** | 重新生成字幕时 `rm -rf` 视频输出子目录 | 剪映内部缓存记住同名文件，导致新字幕在剪映内不生效 | 严禁删除输出目录；让 `generate` 自动递增版本号（如 `_v2`、`_v3`）。 |
| **翻译索引对齐** | 翻译时漏行、合并行或输出非数字 key 的字典 | 导致中英文索引错位（英文对齐正常，中文整批串行） | 严格输出 `{"<str(index)>": "<zh>", ...}`，**必须 100% 覆盖全部 index**。 |
| **漏音补洞 VAD** | 在 `fill_gaps` 漏音补洞流程中强制叠加 `--vad` | 笑声、欢呼或音乐垫底下的真实语音被 VAD 二次抹杀 | `fill_gaps` 恢复解码阶段**恒为裸跑（无 VAD）**，不透传全局 VAD。 |
| **人声分离时长** | 用 ffmpeg/demucs 手动裁切/重采样后再喂给 Whisper，或质疑「分离后时长 ≠ 原视频」为 BUG | 字幕时间戳全局漂移，1s 错位 = 全片报废 | 分离输出**必须** `|dur(out) - dur(orig)| < 50ms`；否则 CLI 自动降级回原音频，不要手改。 |
| **8GB GPU OOM** | 用脚本并行跑 demucs + Whisper，或在同一进程让两模型常驻显存 | RTX 3060/4060 级别必炸 CUDA OOM | CLI 已保证「demucs→释放显存→Whisper」顺序；若需脚本调用，也必须遵守「单一大模型串行」。 |
| **人声分离缓存** | 开了 `--separate-vocals` 又手动 `rm chunk_*.json` 试图「强制重跑 Whisper 但保留 vocals.wav」 | chunk 指纹已嵌入 vsep 参数，删缓存只删一半会让 resegment/fill_gaps 找不到同路径 vocals.wav | 正常跑不用管缓存；真要清空就把输出目录的 `<base>.vocals_*.wav` 和 `chunk_*.json` 一起删，或换个 base。 |
| **工具链查找** | 仅因 `where ffmpeg` 为空便向用户报错停摆 | 忽略了 `.env` 注入机制，造成虚假缺失报错 | 运行 `video-translate doctor`，或按 [TOOLCHAIN.md](TOOLCHAIN.md) 配置 `.env` 中的 `VT_FFMPEG_DIR`。 |
| **Exit Code 6** | 遇到程序退出码 6 时当成错误反复重试 `run` | 死循环卡在转写步骤，无法进入翻译 | 退出码 6 是 `[AWAITING_AGENT]` 挂起信号，表明转写已完成，等待 Agent 执行翻译。 |
| **依赖装错环境** | 把 `demucs`/`torch` 这类重依赖放进 optional extra（`[audio]`）或只写在 `requirements.txt` 却不进 `pyproject` 顶层 `dependencies` | 默认 `pip install -e .` 不装它，依赖飘到系统 Python、venv 里 `import` 不到、`--separate-vocals` 静默降级 | **任何运行时依赖都写进 `pyproject` 顶层 `dependencies`**（不藏 extra）；装环境只用 `pip install -e .` / `uv sync` 一条命令，不依赖额外动作。详见 [TOOLCHAIN.md](TOOLCHAIN.md) §依赖与 wheel 镜像。 |
| **CUDA wheel 装成 CPU 版** | 用 `pip install`（不带 `--index-url`）装 `torch`/`torchaudio`，或以为 `[tool.uv.sources]` 对 pip 生效 | 无代理时 pip 回退 PyPI 默认 `+cpu` wheel，`torch.cuda.is_available()`=False，GPU 加速失效 | **CUDA 版必须走镜像索引**：`uv sync`（认 `[tool.uv.sources]`，自动按平台选 CUDA/CPU wheel）或 `pip install torch --index-url https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cu124/`。绝不裸 `pip install torch`。详见 [TOOLCHAIN.md](TOOLCHAIN.md) §依赖与 wheel 镜像。 |
| **镜像源靠 Agent 临选** | Agent/人工每次安装时现场拼 `--extra-index-url` 或挑代理 | 换人或换机就装不动、或装错源，不可复现 | **镜像源固化进 `pyproject` 的 `[tool.uv.index]`**（cu124→清华镜像）+ `PIP_EXTRA_INDEX_URL` 进 [TOOLCHAIN.md](TOOLCHAIN.md)；安装一律程序决定，不靠临场决策。 |
| **尾部回音幻觉** | 在笑声/欢呼/掌声等"有能量无语义"窗口后，看到新段复述上一句尾部（如真句 `give me a yogurt either way.` 后冒出 `I'm not hungry either way.`）时，手工删段或重算时间戳 | 手工删段破坏 index 对齐、重算时间戳破坏声学层；且下次重跑又复现 | 这是 Whisper 自回归固有缺陷（ADR-020）。**不要手工改**，靠 `drop_hallucination_segments` 自动拦截：段内词与前驱**逐字共享时间戳且含零时长词**（第四信号）即判回音；转写层已携带 `avg_logprob` 供第五信号。两信号已在单测覆盖，全片重跑自动生效。 |

---

## 2. 质量护栏体系 (Quality Guardrails)

字幕质量分解为 **声学层 / 内容层 / 表现层** 三个正交维度（[ADR-012](docs/adr/012-acoustic-timestamp-truth.md) / [Spec 18](docs/specs/18-verify.md)）：

| 维度 | 决策点 | 核心规则 | 依据 |
|---|---|---|---|
| **声学层**（时间轴压在真语音） | VAD 路由 | 依据 `doctor --video` 音频画像**自动路由**，禁止盲猜 | ADR-011 / ADR-012 |
| **声学层** | 漂移与漏检 | 对照 `silencedetect` 独立参照，探测静音跨越与 `uncovered-audio` (≥2s 无 cue 语音窗) | ADR-012 / ADR-016 |
| **声学层** | 幻觉拦截 | `drop_hallucination_segments` 五信号：word 塌缩≥50%+邻居3-gram 重复 / 整段落静音窗 / **尾部回音（窗口被邻居时间窗包含且含零时长词，确定性）/ Whisper 低 `avg_logprob`** | ADR-012 / ADR-020 |
| **内容层**（zh 忠实于 en） | 覆盖与对齐 | `validate_zh`（覆盖率）→ `verify_align`（Pearson 索引对齐）→ 中英混杂词检测 → **语义回读（默认开启）** | Spec 17 / Spec 18 |
| **表现层**（出入字时机） | 显示窗口 | 保持 `tail 0.3 / min-dur 1.0` 默认值；防剪映缓存碰撞自动 `_vN` 递增 | Spec 04 / ADR-012 |

**VAD 路由决策表（由 `doctor` 自动给出）**：
- 干净单人录音 / 朗诵 → `--vad`（段边界钉在真实静音，根除漂移）
- 音乐重 / 低信噪比 / 耳语 → 裸跑（默认）+ `fill_gaps` 自动补洞
- 干净但电平偏低（`mean < -20` 或 `max < -5`）→ 先 `loudnorm` 归一化，再 `--vad --vad-threshold 0.1`
- **混合音频**（干净对话与欢呼/笑声/BGM 交替）→ `--adaptive-vad`（按 chunk 音频画像动态路由，[ADR-015](docs/adr/015-adaptive-per-chunk-vad.md)）
- **强 BGM / 原声带影片 / MV / 演唱会**（[Spec 19](docs/specs/19-vocal-separation.md) / [ADR-017](docs/adr/017-vocal-separation.md)）→ 先 `--separate-vocals` 跑 Demucs 提取人声轨，再按画像裸跑或 `--adaptive-vad`

---

## 3. 标准执行状态机 (Standard Execution Flow)

### Phase 0: 探测与确认 (Preflight)
1. **定位视频**：优先查找 `videos/` 目录；若为空或多文件，与用户确认目标视频。
2. **环境自检**：
   ```bash
   video-translate doctor
   ```
   检查 FFmpeg、CUDA / CPU 设备、模型缓存是否就绪（未就绪参考 [`TOOLCHAIN.md`](TOOLCHAIN.md) 配置 `.env`）。
3. **音频画像、VAD 与人声分离确认**：
   ```bash
   video-translate doctor --video "videos/<video.mp4>"
   ```
   - 检查推荐的 VAD 模式（裸跑 / `--vad` / `--adaptive-vad`）。
   - 若提示 `vocal separation: RECOMMENDED (--separate-vocals)` 或已知视频含强 BGM/多杂音，在 Phase 1 运行时追加 `--separate-vocals`。

---

### Phase 1: 转写与出题 (Transcribe & Emit Task)
执行转写流水线（默认使用 Agent 引擎）：
```bash
# 标准运行
video-translate run "videos/<video.mp4>"

# 强 BGM / 伴奏 / 噪音场景（经 doctor 推荐或人工判断）
video-translate run "videos/<video.mp4>" --separate-vocals
```
- 转写采用分块可续跑设计（`chunk_N.json` 自动断点恢复）。
- 转写 + 断句合并 + 漏音补洞完成后，生成 `<base>.translate_task.json`。
- **程序主动返回 Exit Code 6 (`[AWAITING_AGENT]`) 挂起，等待 Agent 翻译。**

---

### Phase 2: Agent 翻译 (Agent-as-Engine)
作为翻译引擎，Agent 执行以下步骤：
1. 读取 `<base>.translate_task.json`，阅读 `full_transcript` 全局上下文、`source` 背景提示与 `persona` 设定。
2. 逐批翻译 `to_translate` 中的每一项（遵循「信达雅 + 口语感」，保留语气情绪）。
3. 生成 `<base>.zh_segments.json`，格式为严格的 `{"<str(index)>": "<zh>", ...}` 字典，**必须 100% 覆盖所有 index**。
4. （可选）校验覆盖完整性：
   ```bash
   python -c "from video_translate.translate import validate_zh; print(validate_zh('<base>.segments_en.json', '<base>.zh_segments.json'))"
   ```

---

### Phase 3: 字幕生成 (Generate)
```bash
video-translate generate \
    --segments "videos/<base>.segments_en.json" \
    --zh "videos/<base>.zh_segments.json" \
    --outdir "videos/<base>" --base "<base>"
```
- 自动运行 `verify_align` 索引对齐检查（防错行）。
- 输出 4 个核心产物（`.bilingual.srt`、`.zh.srt`、`.en.srt`、`.txt`）。
- 落地于独立的 `<base>/` 子目录，并自动处理 `_vN` 版本递增以规避剪映导入缓存。

---

### Phase 4: 门禁自检与交付 (Verify & Deliver)
运行三 Lane 统一门禁检查（[Spec 18](docs/specs/18-verify.md)）：
```bash
video-translate verify \
    --segments "videos/<base>.segments_en.json" \
    --zh "videos/<base>.zh_segments.json" \
    --video "videos/<video.mp4>"
```
1. **声学 Lane**：对照 `silencedetect` 检查静音重叠与漏检 (`uncovered-audio`)。
2. **内容 Lane**：检查行数覆盖、索引漂移、未翻译英文残留，并生成 `<base>.semantic_reread_task.json` 供 Agent 结合邻居语境快速回读标记。
3. **表现 Lane**：检查显示窗口参数完整性。
4. **交付**：向用户汇报最终字幕路径，剪映导入主文件为 `<base>.bilingual.srt`。

---

## 4. 辅助分支 (Auxiliary Workflows)

### 4.1 全自动无头模式 (`--engine google`)
若无需 Agent 介入的高质量翻译：
```bash
video-translate run "videos/<video.mp4>" --engine google
```
翻译失败项将沉淀至 `<base>.agent_pending.json`。

### 4.2 补录回填 (`backfill`)
针对 Google 引擎未译出的段落：
```bash
# 1. 生成待补任务
video-translate backfill --pending "<base>.agent_pending.json" --out "<base>.zh_segments.json"
# 2. Agent 翻译后保存为 your_zh.json
# 3. 回填并重新生成
video-translate backfill --pending "<base>.agent_pending.json" --out "<base>.zh_segments.json" \
    --agent-zh your_zh.json --segments "<base>.segments_en.json" --outdir "videos/<base>" --base "<base>"
```

### 4.3 局部多语种重转写 (`resegment`)
针对特定时间窗口的混杂语种修正（如预告片中夹杂的日语片段）：
```bash
video-translate resegment --segments "<base>.segments_en.json" --video "<video.mp4>" \
    --windows 12.0-18.5 41.0-45.0 --lang ja
```

---

## 5. 跨工具执行备忘

- **断点续跑原则**：严禁在排查问题时删除 `chunk_*.json` 或 `segments_raw.json`。若只需重跑翻译与生成，使用 `run --skip transcribe`。
- **环境隔离详情**：参见 [`TOOLCHAIN.md`](TOOLCHAIN.md)。
- **历史演进与技术案例**：参见 [`docs/HISTORY.md`](docs/HISTORY.md) 与 [`docs/POSTMORTEM-JamieFoxx.md`](docs/POSTMORTEM-JamieFoxx.md)。