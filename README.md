# video-translate

Turn a video into **Jianying(剪映)-importable bilingual (zh/en) subtitles** with a
faithful, resumable pipeline. This single file holds **both the English and the
中文 documentation** so they never drift apart.

English Documentation → [jump](#english-documentation) ｜ 中文文档 → [jump](#中文文档)

---

## What's new in V3

- **Word-level timestamps** — `segments_en.json` items now carry a
  `words:[{word,start,end}]` field (faster-whisper `word_timestamps=True`).
- **Cue splitting (断行)** — after merge, over-long cues (> `merge_max_chars`,
  default **42**, the 剪映 single-line limit) are split at **word boundaries**;
  real inter-scene silence swallowed by the ASR is split back out so it survives
  into the timeline (**fixes issue #001**). Default ON; `--no-split` disables it;
  `--merge-max-chars` overrides the width.
- **`--gap` (default 0.2s)** on `generate`/`run` — trims trailing silence so
  adjacent cues keep at least `gap` spacing and never overlap; it never fabricates
  silence where the real gap is already larger.
- **Glossary** — `--glossary PATH` (txt/json), env `VT_GLOSSARY`, TOML
  `[translate] glossary` injects a term→译名 map into the translation persona so
  character/proper-noun names stay consistent across episodes (soft guidance, not
  forced replacement).
- **doctor probes Google reachability** — the env check now also verifies the
  Google Translate endpoint via the resolved proxy, so a long transcribe won't
  fail first at the translate step. Default still exits 0 (prints `[MISS]`);
  `--strict` returns exit code **7**.

> Design invariant (unchanged since V1): **timestamps are acoustic facts** produced
> by transcription and are never recomputed downstream — translation only rewrites
> text, and split/gap only trim or cut at real word/silence boundaries.

---

<a id="english-documentation"></a>
## English Documentation

Turn a video into **Jianying(剪映)-importable bilingual (zh/en) subtitles** with a
faithful, resumable pipeline:

```
video ──transcribe──▶ segments_en.json ──translate──▶ zh_segments.json ──generate──▶ *.srt / *.txt
        (faster-whisper                 (Google Translate               (byte-stable
         large-v3, CPU/int8,            via HTTP proxy, or              SRT/TXT, word-level
         word_timestamps)               agent-as-engine)               boundaries + --gap)
```

Design invariant: **timestamps are acoustic facts** produced by transcription and
are never recomputed downstream — translation only rewrites text, so subtitles
stay glued to the audio.

### For AI agents / automation

If you are an agent (WorkBuddy / Claude Code / Cursor / Cline / …) asked to
generate subtitles with this project, **read [`AGENTS.md`](AGENTS.md) first** and
follow its execution protocol (Preflight → run → verify). Do not improvise the
pipeline; the guide encodes the resume, proxy, and golden-verification rules.

### For WorkBuddy users (skill)

A thin WorkBuddy skill can wrap this CLI. To install it, copy the skill folder
into `~/.workbuddy/skills/video-translate/` (user-level) or
`{workspace}/.workbuddy/skills/` (project-level), then invoke it in chat. The
skill delegates all real work to the `video-translate` CLI documented here.

### Quick start (humans)

```bash
# 1. Install (creates .venv, installs deps + this package editable)
make install-dev

# 2. Check your environment (ffmpeg, model cache, proxy, deps, engine, Google reachability)
make doctor

# 3. Download the large-v3 model once (~3GB, reused across projects)
.venv/bin/video-translate setup            # reuses ~/.cache/huggingface if present

# 4. Full pipeline — zero config (base/outdir default from the video path)
.venv/bin/video-translate run "videos/apollo.mp4"             # agent engine (default)
.venv/bin/video-translate run "videos/apollo.mp4" --engine google   # headless end-to-end

# 4b. (agent engine only) lines Google skipped land in <base>.agent_pending.json
.venv/bin/video-translate backfill --pending videos/apollo.agent_pending.json \
    --out videos/apollo.zh_segments.json                      # emits backfill_task.json, exit 6
# ... the agent fills backfill_task.json, then merges + regenerates:
.venv/bin/video-translate backfill --pending videos/apollo.agent_pending.json \
    --out videos/apollo.zh_segments.json --agent-zh videos/apollo.backfill_zh.json \
    --segments videos/apollo.segments_en.json --outdir videos --base apollo

# 5. Import <video_dir>/apollo.bilingual.srt into 剪映
```

V3 defaults: `INPUT` is positional; `--base` = video filename stem; `--outdir` =
the video's own directory; `--lang` auto-detects; `--proxy` auto-detects
(`--no-proxy` for direct). Cue splitting is **ON** by default (`--no-split` to
skip); `--gap` defaults to **0.2s**. The **agent engine** (default) stops after
transcribe + merge + split and emits a translation task for the calling agent
(**exit 6**); use `--engine google` for a fully automatic (lower-quality) run. Run
stages individually with `transcribe` / `translate` / `generate`, or resume a
partial run with `run --skip transcribe`.

> **Exit 6 = the agent's turn.** With `--engine agent`, `run`/`translate` finish
> transcription, write `*.translate_task.json` (or `backfill_task.json`), then exit 6.
> The calling agent reads that file, translates each `to_translate` item per the
> `persona` (and optional glossary), writes `*.zh_segments.json`, then runs
> `generate`. This project embeds **no LLM client** — see
> [ADR-005](docs/adr/005-agent-as-engine.md).

### Requirements

- **Python 3.13** (`.python-version` pins it).
- **ffmpeg + ffprobe** on `PATH`.
- **HTTP proxy** for model download & Google Translate (default
  `http://127.0.0.1:7890`, e.g. Clash). **SOCKS is rejected** — it breaks
  huggingface_hub (see [ADR-003](docs/adr/003-http-proxy-only.md)).
- ~3GB disk for the large-v3 model (shared HF cache at `~/.cache/huggingface`).

### Configuration

Priority: **CLI args > env vars > `.video-translate.toml` > defaults**. See
[Spec 06](docs/specs/06-config.md). Example `.video-translate.toml`:

```toml
[transcribe]
model = "large-v3"
chunk = 240.0
lang  = "auto"          # auto-detect (default)

[translate]
src = "en"
tgt = "zh-CN"
glossary = "glossary.txt"   # V3: consistent 译名 for names/terms

[llm]
persona = "你是一位资深中英字幕译者。遵循「信达雅」+ 口语感……"

[hf]
cache_dir = "~/.cache/huggingface"   # shared model cache

[merge]
merge_enabled   = true
merge_max_dur   = 8.0
merge_max_gap   = 0.5
merge_max_chars = 42     # V3: single-line width for cue splitting (was reserved)
```

Supported TOML sections: `transcribe`, `translate`, `llm`, `hf`, `merge`
(`[hf] cache_dir` maps to `hf_cache_dir`). Environment overrides:

| Env var                 | Maps to              |
|-------------------------|----------------------|
| `VT_MODEL`              | model                |
| `VT_CHUNK`              | chunk                |
| `VT_LANG`               | lang (use `auto` for detect) |
| `VT_PROXY`              | proxy                |
| `VT_SRC` / `VT_TGT`     | src / tgt            |
| `VT_ENGINE`             | engine (`agent`/`google`) |
| `VT_PERSONA`            | persona              |
| `VT_MERGE_MAX_DUR`      | merge_max_dur        |
| `VT_MERGE_MAX_GAP`      | merge_max_gap        |
| `VT_MERGE_MAX_CHARS`    | merge_max_chars (cue split width) |
| `VT_GLOSSARY`           | glossary (V3)        |
| `HF_HOME`               | hf_cache_dir         |
| `HTTPS_PROXY`/`HTTP_PROXY` | proxy (fallback if `VT_PROXY` unset) |

### V3 CLI flags

| Flag (subcommand)            | Default | Meaning                                                        |
|------------------------------|---------|----------------------------------------------------------------|
| `--no-split` (`transcribe`/`run`) | off | disable cue splitting after merge (keep merged cues as-is) |
| `--merge-max-chars N` (`transcribe`/`run`) | 42 | max chars per cue before splitting (剪映 width) |
| `--gap N` (`generate`/`run`) | 0.2 | min gap (s) between cues; trims trailing silence, no overlap |
| `--glossary PATH` (`translate`/`run`) | — | glossary txt/json injected into the translation persona |
| `--strict` (`doctor`) | off | return exit code 7 if any check (incl. Google endpoint) fails |

### Outputs

| File                       | Use                                  |
|----------------------------|--------------------------------------|
| `<base>.bilingual.srt`     | 中文在上 / 英文在下，导入剪映         |
| `<base>.zh.srt`            | 纯中文字幕                            |
| `<base>.en.srt`            | 纯英文字幕                            |
| `<base>.txt`               | 双语校对稿                            |

`segments_en.json` items also carry `words:[{word,start,end}]` (V3) used for
word-level cue windows and silence-preserving splits.

### Exit codes

| Code | Meaning                                                  |
|------|----------------------------------------------------------|
| 0    | success                                                  |
| 1    | runtime error                                            |
| 2    | argument error (argparse)                                |
| 3    | missing dependency (ffmpeg / HF model)                   |
| 4    | proxy error (e.g. SOCKS proxy given)                     |
| 5    | transcription killed (SIGKILL); safe to re-run           |
| 6    | **awaiting agent** — transcribe + task done; agent must translate |
| 7    | doctor `--strict`: a required env check failed (e.g. Google endpoint unreachable) |

Code 6 is the core of the **agent-as-engine** design: the CLI does the
CPU-bound transcription, then hands a translation task to the calling agent and
stops. Non-agent (headless) runs use `--engine google` and never return 6.

### Commands reference

| Command     | Role                                                       |
|-------------|------------------------------------------------------------|
| `run`       | transcribe → translate → generate (full pipeline)         |
| `transcribe`| video → `segments_en.json` (chunked, resumable, + merge + split) |
| `translate` | `segments_en.json` → `zh_segments.json` (agent task / google) |
| `generate`  | `segments_en.json` + `zh_segments.json` → 4 subtitle files |
| `backfill`  | fill `agent_pending.json` and merge + regenerate           |
| `setup`     | check/download the HF model (reuse if cached)             |
| `doctor`    | environment self-check (+ Google endpoint probe)           |

### Development (TDD + SDD)

- **Specs first**: [`docs/specs/`](docs/specs) (00–16) define behavior before code.
- **Decisions**: [`docs/adr/`](docs/adr) records why (CPU/int8, chunked-resume,
  HTTP-proxy-only, segment-merge, agent-as-engine, lang-autodetect, proxy-autodetect,
  silence-preservation, glossary).
- **Design**: [`docs/design/`](docs/design) is the principle-level write-up
  (architecture, word-level alignment, the cue-splitting mechanism, V1→V2→V3 evolution).
- **Tests**: `make test` (fast unit+contract+golden, skips `@slow`); `make test-all`
  (includes the real e2e over the source video). Golden layers:
  `test_generate_golden` (build_outputs byte-exact), `test_merge_golden`
  (merge_segments determinism), `test_v1_golden_preserved` (V1 archived as `.v1`),
  `test_v2_golden_preserved` (V2 archived as `.v2`).

```bash
make test        # ~140 fast tests
make test-all    # + slow e2e (needs model + video)
make clean
```

### Design notes

- **Agent as engine** (V2, [ADR-005](docs/adr/005-agent-as-engine.md)) — the CLI's
  default `--engine agent` emits a translation task for the calling agent (which
  has its own LLM); no LLM client dependency. Google is the `--engine google`
  headless fallback.
- **Segment merge** (V2, [ADR-004](docs/adr/004-segment-merge-strategy.md)) —
  adjacent Whisper fragments rejoin into readable cues; timestamps taken verbatim
  (first start / last end), never recomputed. Default ON (`--no-merge` to skip).
- **Cue splitting** (V3, [Spec 13](docs/specs/13-cue-splitting.md) /
  [ADR-009](docs/adr/009-silence-preservation.md)) — after merge, over-long cues are
  split at word boundaries (剪映 42-char line limit), and real inter-scene silence
  swallowed by the ASR is split back out (issue #001). Word-level boundaries mean
  cues no longer start early (VAD padding dropped). Default ON (`--no-split` to skip).
- **Glossary** (V3, [Spec 14](docs/specs/14-glossary.md) / [ADR-010](docs/adr/010-glossary.md))
  — a soft term→译名 map injected into the persona so names stay consistent; not a
  forced find/replace (preserves 口语感/信达雅).
- **Backfill** (V2) — when `--engine google` leaves untranslated lines, they are
  written to `<base>.agent_pending.json`. `backfill` emits a focused task
  (`backfill_task.json`, exit 6) for the agent, then merges the agent's
  `*.backfill_zh.json` back and regenerates the subtitle files.
- **Resumable transcription** — audio split into `chunk` chunks; each
  `chunk_N.json` is persisted atomically and skipped on re-run
  ([ADR-002](docs/adr/002-chunked-resume.md)).
- **CPU / int8** — CTranslate2 has no AMD/Metal support; forced
  ([ADR-001](docs/adr/001-cpu-int8.md)).
- **Proxy auto-detect** (V2, [ADR-007](docs/adr/007-proxy-autodetect.md)) —
  `--no-proxy` / `--proxy` / env / probe 7890 → direct. SOCKS still rejected
  ([ADR-003](docs/adr/003-http-proxy-only.md)).
- **doctor Google probe** (V3) — verifies the Google Translate endpoint via the
  resolved proxy; default prints `[MISS]` and exits 0, `--strict` returns 7.

---

<a id="中文文档"></a>
## 中文文档

把视频转成**可导入剪映的中英双语字幕**，走一条忠实、可断点续跑的流水线：

```
视频 ──转写──▶ segments_en.json ──翻译──▶ zh_segments.json ──生成──▶ *.srt / *.txt
        (faster-whisper                 (Google 翻译                (字节级稳定
         large-v3, CPU/int8,           走 HTTP 代理，或             SRT/TXT，取词级
         词级时间戳)                     agent 即引擎)              边界 + --gap)
```

设计不变量：**时间戳是声学事实**，由转写阶段产生，下游绝不重算——翻译只改写文本，字幕始终与音频对齐。

### 面向 AI Agent / 自动化

如果你是一个被指派用本项目生成字幕的 Agent（WorkBuddy / Claude Code / Cursor / Cline / …），请**先读 [`AGENTS.md`](AGENTS.md)**，并按其中的执行协议操作（Preflight 探环境 → run 转写 → 校验）。不要即兴改写流水线——该指南里固化了续跑、代理、golden 校验等规则。

### 面向 WorkBuddy 用户（skill）

一个轻量 WorkBuddy skill 可以包住这个 CLI。安装方式：把 skill 文件夹复制到 `~/.workbuddy/skills/video-translate/`（用户级）或 `{workspace}/.workbuddy/skills/`（项目级），然后在对话里调用。skill 把所有真实工作委托给本文档描述的 `video-translate` CLI。

### 快速开始（人工）

```bash
# 1. 安装（创建 .venv，安装依赖 + 本包可编辑模式）
make install-dev

# 2. 自检环境（ffmpeg、模型缓存、代理、依赖、引擎、Google 可达性）
make doctor

# 3. 首次下载 large-v3 模型（约 3GB，跨项目复用）
.venv/bin/video-translate setup            # 若 ~/.cache/huggingface 已有则复用

# 4. 全流程——零配置（base/outdir 默认取自视频路径）
.venv/bin/video-translate run "videos/apollo.mp4"             # agent 引擎（默认）
.venv/bin/video-translate run "videos/apollo.mp4" --engine google   # 无头端到端

# 4b. （仅 agent 引擎）Google 漏翻的行会落到 <base>.agent_pending.json
.venv/bin/video-translate backfill --pending videos/apollo.agent_pending.json \
    --out videos/apollo.zh_segments.json                      # 生成 backfill_task.json，退出码 6
# ...... Agent 填好 backfill_task.json 后，合并并重生成：
.venv/bin/video-translate backfill --pending videos/apollo.agent_pending.json \
    --out videos/apollo.zh_segments.json --agent-zh videos/apollo.backfill_zh.json \
    --segments videos/apollo.segments_en.json --outdir videos --base apollo

# 5. 将 <视频目录>/apollo.bilingual.srt 导入剪映
```

V3 默认值：`INPUT` 是位置参数；`--base` = 视频文件名主干；`--outdir` = 视频所在目录；`--lang` 自动检测；`--proxy` 自动检测（`--no-proxy` 走直连）。**断行（cue split）默认开启**（`--no-split` 可关）；`--gap` 默认 **0.2s**。默认的 **agent 引擎**在转写 + merge + 断行后停下，对外吐一份翻译任务文件给调用方 Agent（**退出码 6**）；想全自动（质量较低）跑则用 `--engine google`。各阶段也可用 `transcribe` / `translate` / `generate` 单独执行，或用 `run --skip transcribe` 续跑上次中断的部分。

> **退出码 6 = 轮到 Agent 了。** 使用 `--engine agent` 时，`run`/`translate` 完成转写后会写出 `*.translate_task.json`（或 `backfill_task.json`），然后以退出码 6 结束。调用方 Agent 读取该文件，按 `persona`（与可选的术语表）把每条 `to_translate` 翻译成中文，写成 `*.zh_segments.json`，再运行 `generate`。本项目**不内置任何 LLM 客户端**——见 [ADR-005](docs/adr/005-agent-as-engine.md)。

### 环境要求

- **Python 3.13**（`.python-version` 已锁定）。
- **ffmpeg + ffprobe** 在 `PATH` 中。
- 模型下载与 Google 翻译需要 **HTTP 代理**（默认 `http://127.0.0.1:7890`，如 Clash）。**不支持 SOCKS**——它会破坏 huggingface_hub（见 [ADR-003](docs/adr/003-http-proxy-only.md)）。
- large-v3 模型约需 3GB 磁盘空间（共享 HF 缓存于 `~/.cache/huggingface`）。

### 配置

优先级：**CLI 参数 > 环境变量 > `.video-translate.toml` > 默认值**。详见 [Spec 06](docs/specs/06-config.md)。`.video-translate.toml` 示例：

```toml
[transcribe]
model = "large-v3"
chunk = 240.0
lang  = "auto"          # 自动检测（默认）

[translate]
src = "en"
tgt = "zh-CN"
glossary = "glossary.txt"   # V3：人名/术语统一译名

[llm]
persona = "你是一位资深中英字幕译者。遵循「信达雅」+ 口语感……"

[hf]
cache_dir = "~/.cache/huggingface"   # 共享模型缓存

[merge]
merge_enabled   = true
merge_max_dur   = 8.0
merge_max_gap   = 0.5
merge_max_chars = 42     # V3：断行单行宽度（此前为保留字段）
```

支持的 TOML 段落：`transcribe`、`translate`、`llm`、`hf`、`merge`（`[hf] cache_dir` 映射到 `hf_cache_dir`）。环境变量覆盖：

| 环境变量                  | 映射到               |
|---------------------------|----------------------|
| `VT_MODEL`                | model                |
| `VT_CHUNK`                | chunk                |
| `VT_LANG`                 | lang（用 `auto` 表示自动检测） |
| `VT_PROXY`                | proxy                |
| `VT_SRC` / `VT_TGT`       | src / tgt            |
| `VT_ENGINE`               | engine（`agent`/`google`） |
| `VT_PERSONA`              | persona              |
| `VT_MERGE_MAX_DUR`        | merge_max_dur        |
| `VT_MERGE_MAX_GAP`        | merge_max_gap        |
| `VT_MERGE_MAX_CHARS`      | merge_max_chars（断行宽度） |
| `VT_GLOSSARY`             | glossary（V3）       |
| `HF_HOME`                 | hf_cache_dir         |
| `HTTPS_PROXY`/`HTTP_PROXY` | proxy（在 `VT_PROXY` 未设时兜底） |

### V3 新增 CLI 参数

| 参数（子命令）               | 默认  | 含义                                                |
|------------------------------|-------|-----------------------------------------------------|
| `--no-split`（`transcribe`/`run`） | 关 | 关闭 merge 后的断行（保留合并后的整段）           |
| `--merge-max-chars N`（`transcribe`/`run`） | 42 | 每行最大字符数（剪映宽度），超过即断行        |
| `--gap N`（`generate`/`run`） | 0.2 | 相邻 cue 最小间隔（秒）；裁掉尾随静音、不重叠    |
| `--glossary PATH`（`translate`/`run`） | — | 术语表（txt/json），注入翻译 persona            |
| `--strict`（`doctor`） | 关 | 任一检查（含 Google 端点）失败则返回退出码 7     |

### 产物

| 文件                       | 用途                                 |
|----------------------------|--------------------------------------|
| `<base>.bilingual.srt`     | 中文在上 / 英文在下，导入剪映         |
| `<base>.zh.srt`            | 纯中文字幕                            |
| `<base>.en.srt`            | 纯英文字幕                            |
| `<base>.txt`              | 双语校对稿                            |

`segments_en.json` 的每一项还带 `words:[{word,start,end}]`（V3），用于词级 cue 窗口与"保留静音"式断行。

### 退出码

| 码   | 含义                                                     |
|------|----------------------------------------------------------|
| 0    | 成功                                                     |
| 1    | 运行时错误                                               |
| 2    | 参数错误（argparse）                                     |
| 3    | 缺少依赖（ffmpeg / HF 模型）                              |
| 4    | 代理错误（如传入 SOCKS 代理）                             |
| 5    | 转写被杀死（SIGKILL）；可安全重跑                         |
| 6    | **等待 Agent** —— 转写 + 任务已就绪，需 Agent 接手翻译    |
| 7    | doctor `--strict`：某项必需环境检查失败（如 Google 端点不可达） |

退出码 6 是 **agent-as-engine** 设计的核心：CLI 负责吃 CPU 的转写，然后把手里的翻译任务交给调用方 Agent 并停下。非 Agent（无头）模式用 `--engine google`，不会返回 6。

### 命令一览

| 命令        | 作用                                                   |
|-------------|--------------------------------------------------------|
| `run`       | 转写 → 翻译 → 生成（完整流水线）                        |
| `transcribe`| 视频 → `segments_en.json`（分块、可续跑、含 merge + 断行） |
| `translate` | `segments_en.json` → `zh_segments.json`（agent 任务 / google） |
| `generate`  | `segments_en.json` + `zh_segments.json` → 4 个字幕文件  |
| `backfill`  | 补全 `agent_pending.json` 并合并重生成                   |
| `setup`     | 检查/下载 HF 模型（已有则复用）                          |
| `doctor`    | 环境自检（含 Google 端点探测）                           |

### 开发（TDD + SDD）

- **先写规格**：[`docs/specs/`](docs/specs)（00–16）在写代码前定义行为。
- **决策记录**：[`docs/adr/`](docs/adr) 记录「为什么」（CPU/int8、分块续跑、仅 HTTP 代理、segment-merge、agent-as-engine、语种自动检测、代理自动检测、保留静音、术语表）。
- **设计文档**：[`docs/design/`](docs/design) 是原理级说明（架构、词级对齐、断句整体机制、V1→V2→V3 演进）。
- **测试**：`make test`（快速单测 + 契约 + golden，跳过 `@slow`）；`make test-all`（含基于源视频的真实 e2e）。golden 分层：`test_generate_golden`（build_outputs 字节级一致）、`test_merge_golden`（merge_segments 确定性）、`test_v1_golden_preserved`（V1 归档为 `.v1`）、`test_v2_golden_preserved`（V2 归档为 `.v2`）。

```bash
make test        # 约 140 个快测
make test-all    # + 慢速 e2e（需模型 + 视频）
make clean
```

### 设计说明

- **Agent 即引擎**（V2，[ADR-005](docs/adr/005-agent-as-engine.md)）— 默认的 `--engine agent` 对外吐一份翻译任务给调用方 Agent（它自带 LLM），CLI 不依赖任何 LLM 客户端。Google 是 `--engine google` 无头兜底。
- **片段合并**（V2，[ADR-004](docs/adr/004-segment-merge-strategy.md)）— 相邻的 Whisper 碎 cue 重新拼成可读字幕块；时间戳原样取用（首段 start / 末段 end），绝不重算。默认开启（`--no-merge` 可跳过）。
- **断行（cue split）**（V3，[Spec 13](docs/specs/13-cue-splitting.md) / [ADR-009](docs/adr/009-silence-preservation.md)）— merge 之后，过长的 cue 按**词边界**断开（剪映单行 42 字上限），被 ASR 吞掉的真实场景间静音也会被拆出来（issue #001）。词级边界让 cue 不再"提前"出现（去掉 VAD 填充的静音）。默认开启（`--no-split` 可关）。
- **术语表**（V3，[Spec 14](docs/specs/14-glossary.md) / [ADR-010](docs/adr/010-glossary.md)）— 一份柔性的"术语→译名"映射注入 persona，保证全片人名/术语一致；不是强制替换（保留口语感/信达雅）。
- **Backfill**（V2）— 当 `--engine google` 留下未翻译的行时，会写入 `<base>.agent_pending.json`。`backfill` 先生成一份聚焦任务（`backfill_task.json`，退出码 6）交给 Agent，随后把 Agent 填好的 `*.backfill_zh.json` 合并回去并重生成字幕文件。
- **可续跑转写** — 音频按 `chunk` 切分；每个 `chunk_N.json` 原子落盘，重跑时跳过（[ADR-002](docs/adr/002-chunked-resume.md)）。
- **CPU / int8** — CTranslate2 无 AMD/Metal 支持，强制使用（[ADR-001](docs/adr/001-cpu-int8.md)）。
- **代理自动检测**（V2，[ADR-007](docs/adr/007-proxy-autodetect.md)）— `--no-proxy` / `--proxy` / 环境变量 / 探测 7890 → 直连。SOCKS 仍不支持（[ADR-003](docs/adr/003-http-proxy-only.md)）。
- **doctor 探测 Google 端点**（V3）— 经解析出的代理探测 Google 翻译端点可达性；默认打印 `[MISS]` 并退出 0，`--strict` 返回 7。

## License

Private project. See repository owner.
