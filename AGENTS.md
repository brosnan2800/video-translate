# AGENTS.md — Execution guide for AI agents (current)

You are an AI agent asked to turn a video into bilingual (zh/en) subtitles using
this project. Follow this protocol. It is tool-agnostic (WorkBuddy, Claude Code,
Cursor, Cline, plain shell). **Do not reinvent the pipeline** — the rules below
encode resume-safety, proxy correctness, the agent-as-engine translation step,
and output verification.

Read order: this file → [`docs/specs/00-overview.md`](docs/specs/00-overview.md)
for behavior → [`docs/adr/`](docs/adr) for the "why".

> 逐版本变更史（V3–V13 的 battle-tested 操作过程）已移出本文，见
> [`docs/HISTORY.md`](docs/HISTORY.md)。本文只保留**当前真相**与铁律。

---

## 项目铁律（cross-tool hard rules — 任何工具 / 任何 Agent 都必须遵守）

1. **本协议（AGENTS.md）是项目唯一权威。** 开工前先 Read 本文件；不要用各自记忆替代。
   README 是指路口，AGENTS.md 是规则本身。换工具（Claude Code / Codex / Trae / Cursor）
   也只读这个文件，不依赖 WorkBuddy 的私有 memory。
2. **每次提交必须同步文档。** 改代码的同时，必须更新 `AGENTS.md` + `README.md` +
   相关 `docs/specs`、`docs/adr`；只改代码不更新文档的提交视为**未完成**，不得合并/推送。
3. **改代码始终遵循 SDD + TDD。** 先写/更新 spec（行为）+ ADR（决策），再用测试护航
   （`make test` 必须 green，golden 测试保字节级稳定）。

> 注意：`.workbuddy/`（含 memory/skills）是 WorkBuddy 私有数据，**永不提交**；
> 已写入 `.gitignore`。跨工具可移植的规则只能落在仓库文件（AGENTS.md / README / docs）里。

---

## 当前真相速查（current truth at a glance）

字幕正确性 = **声学层 / 内容层 / 表现层** 三个正交维度（[ADR-012](docs/adr/012-acoustic-timestamp-truth.md) /
[Spec 18](docs/specs/18-verify.md)）。不要揉成一团。

| 维度 | 决策点 | 规则 | 依据 |
|---|---|---|---|
| **声学层**（时间轴是否压在真语音） | VAD 开关 | 由 `doctor` 音频画像**自动路由**，不要临场拍脑袋 | ADR-011 / ADR-012 |
| **声学层** | 漂移检测 | 对齐必须对照 `silencedetect` 独立参照校验，whisper 自带时间戳≠声学事实 | ADR-012 / Spec 18 |
| **内容层**（zh 是否忠实 en） | 覆盖 + 索引 + 语义 | `validate_zh`（覆盖）→ `verify_align`（索引，generate 内自动）→ 语义回读（agent 侧，`verify --semantic`） | Spec 17 / Spec 18 |
| **表现层**（字幕何时出/收） | 显示窗 | 默认 `tail 0.3 / min-dur 1.0`，**不得人为 `--tail 0 --min-dur 0` 收紧**；`offset` 仅修正「早出现」 | Spec 04/15 / ADR-012 |

**VAD 路由矩阵（doctor 自动给出，亦可人工确认）**：
- 干净单人录音 / 朗诵 → `--vad`（段边界钉在真实静音，根治漂移）
- 音乐重 / 低信噪比 / 耳语 → 裸跑（默认）+ `fill_gaps`
- 干净但电平偏低（`mean<-20` 或 `max<-5`）→ `--vad --vad-threshold 0.1`（先 `loudnorm`）

**自检门**：生成后跑 `verify --video <video>`（[Spec 18](docs/specs/18-verify.md)）——
声学 lane（silencedetect 对照）、内容 lane（复用 validate_zh + verify_align）、表现 lane
（tail/min-dur 被削 sanity）。

---

## 0. Trigger & video interaction (conversational)

When the user asks to translate/subtitle a video ("翻译这个视频 / 生成字幕 /
做中英字幕"):

1. **Find the video.** First look in the project's `videos/` directory.
   - If one video is there, use it.
   - If several, list them and ask the user which one.
   - If `videos/` is empty or missing, ask the user for the path. Supported
     formats: anything ffmpeg handles (mp4/mov/mkv/webm/avi/m4a/mp3/wav/flac…).
2. **Probe + confirm.** Run `ffprobe` to get duration and detect the audio
   language; show the user a one-line summary ("检测到 12 分钟、英文音轨") and
   **wait for confirmation** ("开始？") before proceeding.
3. **Default base/outdir.** `--base` defaults to the video filename stem;
   `--outdir` defaults to the video's own directory. Override only if the user
   asks.

## 1. Preflight (always run first)

```bash
cd <project-root>
test -f pyproject.toml && echo "in project root: ok"
.venv/bin/video-translate doctor    # or: make doctor
```

`doctor` reports ffmpeg/ffprobe, HF cache, model cached, deps, V2 defaults
(`engine: agent`, `lang: auto-detect`, `proxy: auto-detect`), and — since
ADR-012 — an **audio profile** (volumedetect level + silencedetect gaps) with a
**VAD routing recommendation** (`--vad` / bare / `--vad-threshold 0.1`).

Decide from the report:
- **ffmpeg/ffprobe MISS** → stop; ask the human to `brew install ffmpeg`.
- **faster-whisper NOT installed** → run `make install-dev`.
- **model NOT cached** → run `video-translate setup` (downloads large-v3 once,
  ~3GB, into the shared `~/.cache/huggingface`; reused next time).
- **proxy** → auto-detected. If the user has no proxy and needs Google/network,
  confirm direct egress works, or have them start Clash (7890). `--no-proxy`
  forces direct (fine for transcribe-only when the model is cached).

## 2. Run the pipeline (agent engine, default)

The CLI's default `--engine agent` does NOT translate itself — it transcribes,
merges, emits a translation task, and stops at **exit 6** for you to translate.

```bash
.venv/bin/video-translate run "<video>"           # base/outdir auto from video path
# → produces <video_dir>/<base>.segments_en.json (merged)
#   + <video_dir>/<base>.translate_task.json
#   exit 6: [AWAITING_AGENT]
```

Exit 6 means: **transcribe + merge done; YOU translate now.**

### Your translation step (you are the engine)
1. Read `<base>.translate_task.json`. It has `persona`, `batches[]` each with
   `context_before` / `to_translate` / `context_after`, and an `output_schema`.
2. For each batch, translate the `to_translate` items to Chinese **following the
   persona** (信达雅 + 口语感 — aim for the soul of the sentence, not word-for-word).
   Use the context_before/after for coherence.
3. Write `<base>.zh_segments.json` — a JSON object `{"<str(index)": "<zh>", …}`
   covering **every** index in `to_translate[*].index`.
4. (Optional sanity check) `.venv/bin/python -c "from video_translate.translate import validate_zh; print(validate_zh('<base>.segments_en.json', '<base>.zh_segments.json'))"`.
5. Generate the subtitles:
   ```bash
   .venv/bin/video-translate generate \
       --segments <base>.segments_en.json --zh <base>.zh_segments.json \
       --outdir <video_dir> --base <base>
   ```

   #### 纯文本诗歌翻译（纯文本，非带轴字幕）
   当待译内容是**纯文学诗歌**（不嵌入视频、无时间轴约束）时，在「信达雅 + 口语感」的
   散文基线之上，额外遵循 **许渊冲三美论**：

   - **意美（神似）**：抓原诗灵魂与意境，不逐字死译；隐喻、象征、双关必须保留，
     找不到中文近似双关时标注原文字面 + 意涵，而非硬翻。
   - **音美（可诵）**：保留原诗的押韵 scheme、节奏、顿挫；中文译文音节数尽量贴近
     原句，便于吟诵。此场景**无时间轴**，韵律与形式可完整保留，不必为同步破韵。
   - **形美（形似）**：行数、节、句式、诗体（古体 / 近体 / 自由诗）尽量对应。

   并遵循 **形似 / 神似辩证**：直译保形、意译保神，二者冲突时以「意美」优先，但不得
   牺牲音美与形美到不可诵、不可辨诗体的程度。

   文化专有项（典故、用典）密度显著高于口语场景，须有固定处理规则：保留原典 + 注，
   或替换为本土等价意象，不得逐句即兴。

   > 注意：本条仅适用于**纯文本诗歌**。带时间轴的字幕（含夹带歌词 / rap）遵循上文
   > 散文 / 口语规范，且「时间轴同步」优先级高于「雅 / 形美」——二者不可混用。

   ### Headless alternative (`--engine google`)
If no agent translation is wanted (lower quality, fully automatic):
```bash
.venv/bin/video-translate run "<video>" --engine google
```
Runs transcribe → Google MT → generate end-to-end. Failures land in
`<base>.agent_pending.json`.

## 3. Backfill (if Google failed some segments)

```bash
# 1. prepare a task for just the failed segments (original indices preserved)
.venv/bin/video-translate backfill --pending <base>.agent_pending.json \
    --out <base>.zh_segments.json
# → exit 6: [AWAITING_AGENT], writes <base>.backfill_task.json

# 2. you translate the backfill task -> your_zh.json ({"<orig_index>": "<zh>"})
# 3. merge + regenerate
.venv/bin/video-translate backfill --pending <base>.agent_pending.json \
    --out <base>.zh_segments.json --agent-zh your_zh.json \
    --segments <base>.segments_en.json --outdir <video_dir> --base <base>
```

## 4. Resume rules (do NOT delete intermediates)
- Transcription is **chunked & resumable**: `chunk_N.json` files in `outdir` are
  the checkpoints. Re-running the same command skips completed chunks.
- Merge copies raw to `<base>.segments_raw.json` then writes merged to
  `<base>.segments_en.json`. `--no-merge` skips merge (raw stays as `segments_en`).
- Translation (`--engine google`) checkpoints `zh_segments.json` every 10 segments.
- To reuse existing transcription and only redo later stages: `run --skip transcribe`.

## 5. Verify before you report done
- **Run the unified self-check gate** (Spec 18):
  ```bash
  .venv/bin/video-translate verify --segments <base>.segments_en.json \
      --zh <base>.zh_segments.json --video <video>
  ```
  Three lanes, reported per layer: **acoustic** (silencedetect vs each cue —
  flags "in-silence / cross-silence / first-cue-early"), **content** (reuses
  `validate_zh` + `verify_align`), **presentation** (flags `tail/min-dur` stripped
  to 0). Any flag → fix before delivering. Add `--strict` to fail CI on warnings.
- **Existence**: four files `<base>.{bilingual.srt,zh.srt,en.srt,txt}`.
- **Alignment sanity**: first cue's timestamp matches the first merged segment;
  timestamps monotonic, never negative. (The acoustic lane of `verify` now guards
  this against the *independent* silencedetect reference, not whisper self-assertion.)
- **Completeness** (`--engine google`): `agent_pending.json` is empty or backfilled.
- **Regression (if you touched code)**: `make test` green. Golden tests:
  `test_generate_golden` (build_outputs byte-exact), `test_merge_golden` (merge
  determinism), `test_v1_golden_preserved` (V1 archived as `.v1`).

## 6. Deliver
Report the four output paths to the human and note anything you backfilled. The
importable file for 剪映 is `<base>.bilingual.srt`.

---

## Hard rules (violating these produces broken subtitles)
1. Never recompute timestamps in merge/translate/generate — copy them verbatim.
   Merge takes group's first start / last end (real acoustic boundaries).
2. HTTP proxy only; SOCKS is rejected. `--no-proxy` = direct.
3. Device is cpu/int8 (ADR-001).
4. Never delete `chunk_N.json` / `segments_raw.json` / partial JSON to "clean up"
   — you destroy resume.
5. zh index is 0-based in the map; segment `i` (1-based) reads `zh[i-1]`.
6. Exit 6 = awaiting your translation; not an error. Don't retry the same `run`
   expecting a different result — translate the task and `generate`.
7. **Presentation window defaults are load-bearing — never over-tighten.**
   `--tail` defaults to `0.3` and `--min-dur` to `1.0` (perceived breathing room);
   do **not** pass `--tail 0 --min-dur 0` to "tighten" — that makes subtitles
   appear early / vanish before the speech ends. `--offset` only shifts the display
   window later to fix "subtitle ahead of speech"; it never touches acoustic
   timestamps (ADR-012).
8. **VAD on/off is routed by `doctor`'s audio profile — don't guess.**
   Content type decides: clean studio recording / recitation → `--vad` (anchors
   segment boundaries to real silence, kills drift); music-heavy / low-SNR /
   whisper → bare (default) + `fill_gaps`. See the routing matrix in
   "当前真相速查" above (ADR-011 / ADR-012).

See [`docs/specs/07-gotchas.md`](docs/specs/07-gotchas.md) for the full list.

---


---

## 现行操作铁律（consolidated operating truth）

> 完整逐版本变更史（V3–V13 的 battle-tested 操作过程、案例、踩坑叙事）已移出本文，见
> [`docs/HISTORY.md`](docs/HISTORY.md)。以下只列**当前仍在生效、必须照做**的操作铁律
> （源自 V3–V13，已凝练；决策依据见对应 ADR/Spec）。

### 转写 / VAD（ADR-011）
- VAD 选开，默认裸跑（`vad_filter=False`）。干净录音 / 朗诵显式 `--vad`（段边界钉在真实
  静音，根治漂移）；音乐重 / 低信噪比 / 耳语裸跑 + `fill_gaps`。
- 干净但电平低（`mean_volume < -20` 或 `max_volume < -5`）：先
  `loudnorm=I=-16:TP=-1.5:LRA=11 -c:v copy -c:a aac`，再 `--vad-threshold 0.1`。
- VAD-over-split 幻觉：VAD 把瞬时环境切成短碎片、whisper 幻觉 filler → 必须与裸跑
  `word_timestamps=True` 交叉验证，不轻信。

### 幻觉铁律（V4 / V7 / ADR-012）
- `drop_hallucination_segments` 双信号（词塌陷 ≥50% 且 邻居 3-gram）；ADR-012 增补
  「段整体落在静音窗」孤立幻觉信号（head 静音里的无人机型号类，原双信号漏过）。
- 单窗口塞入反常多文本（如 0.64s 装 16 字）= 低置信幻觉，直接剔除，不翻译。

### 补洞 / fill_gaps（V11 / Spec 16，ADR-012 修订）
- 段间空洞 >8s 强解；段内塌陷（cps<中位*0.45 且 dur≥4s）疑点；prefix collapse 用多 pad
  择优；echo 用 `difflib.SequenceMatcher > 0.7` 跳过；审计非一次性，多轮直到无新洞。
- **ADR-012 修订**：HEAD / TAIL **纯静音窗** 不强制解码（先 `silencedetect` 判真静音），
  堵「恢复出孤立幻觉」的洞。

### zh/en 索引漂移（V12 / Spec 17）
- `verify_align` 在 `generate` 内自动跑（warning-only，`--no-align-check` 关）。
- 铁律 A：逐批按 index 翻译必须有跨模态一致性校验；铁律 B：复用旧译文前先验旧译文本身
  对齐。

### 编排（V13）
- agent 引擎先定；`--engine agent`（默认）完全无网络 / 代理依赖（ADR-005）。
- 统一自检门：`verify` 命令（Spec 18）——声学 / 内容 / 表现三 lane，详见 §5。

### 再生成纪律（V7 血泪）
- 多次 `generate` 改内容时，**绝不 `rm -rf` 输出子文件夹**绕过碰撞 bump（会让文件名停
  在首版无后缀、剪映命中旧缓存）；保留旧文件夹让 `generate` 自动 bump `_vN`。
- 归一化后参数未变但音频变了 → 旧的 chunk 缓存指纹相同会被误用 → 手动清
  `videos/<name>.<fp>.chunk_*.json` 再跑。

### 译文方法（V7）
- 已知影视务必 WebSearch 查原文台词（搜独特一句）。优先级：原文台词 > 语境推断 > 音近。
  改 `segment.text` 修正英文行即可，时间戳不变。
