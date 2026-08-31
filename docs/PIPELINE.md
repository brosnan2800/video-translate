# PIPELINE.md — 标准执行流程（外置版）

> **目的**：把"流程靠人脑记忆 / agent 自觉走完"变成"流程外置成文件 + 每阶段有闸门"。
> 你不用记步骤，agent 也不能假装走完——因为每个阶段必须产出文件，缺一个下游闸门立刻红。
> 本文件是 `AGENTS.md` 的**执行视图**：`AGENTS.md` 讲"为什么"，本文件讲"每一步敲什么、产出什么、过哪道闸"。

## 铁律（来自 AGENTS.md 红线，全程适用）

| # | 不可违背 | 违反后果 | 闸门在哪 |
|---|---|---|---|
| R1 | 声学时间戳只携带、绝不重算；下游只改文本不改时间轴 | 字幕与人声错位 | `verify` 声学 Lane（gate.py acoustic） |
| R2 | 对齐（`--align auto`，T4）后必须重译（段数可能变化） | 中英错行串行 | `check-translate` 闸门（gate.py content） |
| R3 | 禁止删 `chunk_*.json` / 断点缓存；重跑翻译用 `run --skip transcribe` | 长视频全重跑 | 运维纪律（stage-order 不拦，靠纪律） |
| R4 | 翻译严格 `{"<str(index)>": "<zh>"}`，100% 覆盖所有 index | 中英文索引错位 | `check-translate` 闸门 |
| R5 | 表现层窗口保持 `tail 0.3 / min-dur 1.0` | 字幕一闪而过 | `verify` 表现 Lane（gate.py presentation） |
| R6 | 禁止删输出目录；`generate` 自动 `_vN` 递增 | 剪映缓存碰撞 | 运维纪律 |
| R7 | **所有命令一律 `uv run ...`**，绝不用裸 `python` / `video-translate` / `make` | 命中旧全局环境 | 命令前缀（见下） |
| R8 | 退出码 6 = `[AWAITING_AGENT]` 挂起（转写完成等翻译），**不是错误** | 死循环卡转写 | 转写步骤已知 |
| R9 | **禁止跳过 Phase 0 签字**：人声分离 / 风格必须人工拍板后才可转写 | 地基选错，全链重跑 | `preflight_decision.py assert`（退出码 3） |
| R10 | **verify 红线命中必须落地**：声学/确定性内容自动修（带 retry 上限），语义命中 / retry 超限转半自动，绝无限循环 | 死循环耗人 / 静默放行 | `verify_gate.py`（退 3）+ `make verify-approve` |

## 阶段总览

```
Preflight ──▶ 【签字闸门】──▶ Transcribe ──▶ Translate(Agent) ──▶ Generate ──▶ Verify
  (doctor)     人工拍板签字      (run,码6)      (产出zh_segments)    (generate)   (三维门禁)
  +决策单       未签字进不去       │                                   │
                                  └──────── run --skip transcribe ────┘  (仅重跑翻译/生成时)
```

> **两道人工闸门**：首道在 Preflight 之后（拍**意图与地基**：人声分离/风格，选错要重跑全链），
> 末道在 Verify（审**艺术**：中文顺不顺，选错只需局部重译）。中间全交机器，你不用盯。

每个阶段：目标 → 命令（`uv run`）→ 输入 → 产出 → 闸门（不过不许进下一阶段）。

---

## Stage 0 · Preflight（探测与确认）

- **目标**：环境一步到位，禁止自由发挥配环境。
- **命令**：
  ```bash
  uv run video-translate setup          # uv sync + 预拉 large-v3 模型
  uv run video-translate doctor                       # 命令入口/FFmpeg/CUDA/模型 全绿
  uv run video-translate doctor --video "videos/<video.mp4>"   # 音频画像 + VAD 路由建议
  ```
- **产出**：`doctor` 全绿。
- **闸门**：`doctor` 非全绿不许进 Stage 0.5。FFmpeg `[MISS]` 跑 `setup --ffmpeg`，不要全盘搜。

> ⚠️ **不要只把 `doctor` 的建议"记下来"**——那正是失控的源头：建议只是打印，忘了传参就白建议。
> 建议必须走 Stage 0.5 落成签字过的决策单，由程序自动注入参数。

## Stage 0.5 · Decision（人工签字闸门，R9）

- **目标**：把「程序算得出的建议」+「只有人能定的意图」冻结成一份签字过的决策单，
  并让 Stage 1 的命令行参数**由决策单渲染**，不靠人记得传。
- **命令**：
  ```bash
  uv run python gates/preflight_decision.py propose --base <base> --video "videos/<video.mp4>"
  uv run python gates/preflight_decision.py show    --base <base>      # 看建议/理由/待拍板项
  uv run python gates/preflight_decision.py set     --base <base> --item separate_vocals --value true
  uv run python gates/preflight_decision.py set     --base <base> --item style --value literal
  uv run python gates/preflight_decision.py confirm --base <base>      # 签字冻结
  ```
- **产出**：`videos/{base}.preflight_decision.json`（含视频指纹 + `confirmed_flags`）。
- **决策项**：`separate_vocals`（关键）、`style`（关键）、`vad_mode`（非关键）。
  新增项见 `advisors/ADVISORS.md`——**新建一个 `advisor_*.py` 即可，不改任何旧代码**。
- **闸门**：
  - 关键项未拍板 → `confirm` 报 `GATE VIOLATION`，**退出码 3**，签不了字。
  - 未签字 → Stage 1 的 `assert` 报 `GATE VIOLATION`，**退出码 3**，转写进不去。
  - 签字后素材变更（size/mtime 变）→ `STALE DECISION`，**退出码 4**，防止拿上一个视频的决策跑这一个。
  - 签字后又改任一项 → 签字自动失效，必须重签。

## Stage 1 · Transcribe（转写与出题）

- **目标**：产出待翻译任务文件，挂起等 Agent 翻译。
- **前置闸门（必跑，R9）**：
  ```bash
  uv run python gates/preflight_decision.py assert --base <base> --video "videos/<video.mp4>"
  ```
- **命令**（参数由决策单渲染注入，**不要手输 flag**）：
  ```bash
  uv run video-translate run "videos/<video.mp4>" \
      $(uv run python gates/preflight_decision.py render-flags --base <base>)
  # 或直接: make transcribe VIDEO=videos/<video.mp4> BASE=<base>
  ```
- **输入**：视频文件 + 已签字的决策单。
- **产出**：`{base}.translate_task.json`（Agent 翻译任务）、`{base}.segments_en.json`、分块缓存 `chunk_N.json`。
- **已知行为**：程序主动返回 **退出码 6 = AWAITING_AGENT**（正常挂起），不要当错误重试。
- **闸门**：本阶段无需额外闸门；进入 Stage 2 前确认 `segments_en.json` 存在（gate.py stage-order）。
- **对齐提醒（R2）**：GPU 机器默认走 WhisperX 对齐，段边界收紧、merge 可能改变段数 → **Stage 2 必须重译**。

## Stage 2 · Translate（Agent-as-Engine，人工/Agent 步骤）

- **目标**：产出 100% 覆盖 index 的中文翻译。
- **手工/Agent 动作**：
  1. 读 `{base}.translate_task.json`（全局上下文 `full_transcript` + `persona` + `guidelines`）。
  2. 逐批翻译 `to_translate`，遵循风格（`film` / `literal` / `bilingual_study`）。
  3. 写 `{base}.zh_segments.json`，格式严格 `{"<str(index)>": "<zh>", ...}`。
- **产出**：`{base}.zh_segments.json`。
- **闸门（必过，否则不许 generate）**：
  ```bash
  uv run python gates/gate.py content --base <base>     # 校验 100% 覆盖 + 索引对齐
  ```
  失败 = 漏行 / 合并行 / 非数字 key / 对齐后未重译。

## Stage 3 · Generate（字幕生成）

- **目标**：渲染双语字幕，自动 `_vN` 递增防缓存碰撞。
- **命令**（依赖 Stage 2 闸门通过）：
  ```bash
  uv run video-translate generate \
      --segments "videos/<base>.segments_en.json" \
      --zh "videos/<base>.zh_segments.json" \
      --outdir "videos" --base "<base>"
  ```
- **产出**：`{base}/` 子目录下 `.bilingual.srt` / `.zh.srt` / `.en.srt` / `.txt`。
- **闸门**：`generate` 自动跑 `verify_align` 索引对齐检查（防错行），并在末尾自动跑 `verify`（声学/表现层）。

## Stage 4 · Verify（三维门禁自检 + 硬闸门 + 半自动恢复，R10）

- **目标**：三 Lane 统一门禁，全部绿灯才交付；红线命中按"声学/确定性内容自动修、语义转半自动"分流，**带 retry 上限，绝不无限循环**。
- **命令**（单次校验 + 写 gate 状态；自动恢复环见 `make verify-fix`）：
  ```bash
  uv run python gates/verify_gate.py run --base <base> --video <video.mp4>
  #     全绿 → 写 .vt_verify_gate.json breached=false → 退 0
  #     命中 → 分流（见下）→ 写 breached=true, mode=half_auto → 退 3
  ```
- **三 Lane**（来自真实仓库 `verify.py`，三正交维度）：
  - **声学 Lane**（R1）：对照 `silencedetect` 查静音重叠 / `uncovered-audio`（≥2s 无 cue）。**确定性、可自动修**。
  - **内容 Lane**（R2/R4）：行数覆盖、索引漂移、未译英文残留，以及**语义回读 fidelity**。**前三者确定性可自动修；语义 fidelity 概率性、需 LLM、转半自动**。
  - **表现 Lane**（R5）：显示窗口参数完整性（`tail 0.3 / min-dur 1.0`）。
- **分流逻辑（R10）**：
  - 声学 / 确定性内容命中 → `verify_gate.py` 自动补该段（translate + generate）→ 重 verify → `retry++`；`retry ≥ 2` → 熔断转半自动。
  - 语义回读 fidelity 命中 → **不进自动环**（修了可能更差），直接转半自动，不消耗 retry。
  - 语义回读是**半独立 agent 子任务**：`verify.py` 产 `<base>.semantic_reread_task.json` → 你的 agent 回读 → 产 `<base>.semantic_reread_result.json` → **`verify_gate.py` 必须消费这个 result**（命中即退半自动）。现状"产了就完了、不检查、不退码"正是缺这最后一步。
- **交付 / 半自动落点**：
  - 全绿 → 汇报 `{base}.bilingual.srt`（剪映导入主文件），可交付。
  - `breached=true, mode=half_auto` → 停自动环，退 3；你二选一：
    - `make verify-approve BASE=<base>`（人工确认放行），或
    - 看报告后 `make finish` 手动重跑对应段。
  - **绝不发生**：默认放行（非 `--strict` 也退非 0）、无限循环（retry 上限 2）、只产不消费语义回读。

> 完整硬代码落点、函数签名、JSON schema、retry 逻辑、4 项 smoke test 见 **`GAPB_IMPLEMENTATION.md`**（给 coding agent / CodeBuddy 的实现手册）。

---

## 辅助分支

- **全自动无头**：`uv run video-translate run "videos/<v>.mp4" --engine google`（失败沉淀 `{base}.agent_pending.json`）。
- **补录回填**：`uv run video-translate backfill --pending ... --out ...`，Agent 译后回填重生成。
- **局部重转写**：`uv run video-translate resegment --segments ... --video ... --windows 12.0-18.5 --lang ja`。

## 用法速记（Makefile 驱动）

```bash
make preflight VIDEO=videos/x.mp4              # doctor + 生成决策单
make decide-show BASE=x                        # 看建议/理由/待拍板项
make decide BASE=x ITEM=separate_vocals VALUE=true
make decide BASE=x ITEM=style           VALUE=literal
make confirm BASE=x                            # 签字（关键项未拍板 -> 退出码 3）
make transcribe VIDEO=videos/x.mp4 BASE=x      # 未签字进不来；参数自动注入；预期退出码 6
# <- 在此做 Stage 2 翻译，产出 zh_segments.json
make finish VIDEO=videos/x.mp4 BASE=x          # check-translate -> generate -> verify
make ci                                        # pytest + 三维闸门（pre-commit / CI）
```

## Gap B 已设计（Stage 4 从报告型升级为半自动硬闸门）

Stage 4 不再是"报告型 + 默认放行"。已落地为**分流 + 熔断 + 半自动**的硬闸门（见 R10 与上方 Stage 4）。设计要点：

| 检查项 | 旧现状（缺口） | 新设计（Gap B） |
|---|---|---|
| `uncovered-audio`（有声音无字幕） | 只写报告，不自动补洞 | 声学 Lane 确定性，**自动补该段 translate+generate** → 重 verify → retry++；超 2 次熔断转半自动 |
| 语义回读 fidelity | 只产 `.json`，不退码、不消费 | 半独立 agent 子任务；产 `semantic_reread_task.json` → 回读 → 产 `semantic_reread_result.json` → **gate 必须消费**，命中即退半自动（不消耗 retry） |
| 自动恢复环路 | 无 | 有，但 **retry 上限 = 2**，超限即熔断，绝无限循环 |

**硬代码落点**：新增 `gates/verify_gate.py`；改 `gates/checkpoint.py`（`verify` 标 `GATED`）；改 `Makefile`（`make verify-fix` / `make verify-approve`）；改真实仓库 `verify.py`（默认阻断 + 自动触发 + 消费语义回读 result）。

完整实现手册（文件 / 函数 / 常量 / JSON schema / 4 项 smoke test）：**`GAPB_IMPLEMENTATION.md`**。
