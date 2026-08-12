# video-translate

Turn a video into **Jianying(剪映)-importable bilingual (zh/en) subtitles** with a
faithful, resumable pipeline. This single file holds **both the English and the
中文 documentation** so they never drift apart.

**Highlights / 核心特性**
- Video → Jianying(剪映)-importable bilingual (zh/en) subtitles with audio-accurate alignment — 视频转剪映中英双语字幕，字幕与音频精确对齐
- Agent-as-engine: the CLI does the transcription and hands a translation task to the calling Agent (no bundled LLM); `--engine google` is the headless fallback — Agent 即引擎：CLI 负责转写，翻译任务交给调用方 Agent（无内置 LLM），`--engine google` 无头兜底
- faster-whisper `large-v3` on CPU/int8, chunked and resumable — faster-whisper large-v3，纯 CPU/int8，分块可续跑
- V4 transcription quality (beam search / anti-hallucination) + V5 versioned output + V6 drift-snap & scene-context translation — V4 转写质量（束搜索 / 抗幻觉）+ V5 版本化输出 + V6 漂移吸附 / 场景上下文翻译
- V7–V13 hardening (music-heavy VAD-off, mixed-language auto, big-segment gap-fill, zh/en alignment guard) — V7–V13 加固（音乐重关 VAD、混合语种 auto、大段漏字补洞、中英文索引漂移护栏）
- HTTP-proxy only (Clash); SOCKS is rejected — 仅支持 HTTP 代理（Clash），不支持 SOCKS

> **📖 [中文文档 ↓](#中文文档)** &nbsp;|&nbsp; English Documentation → [jump](#english-documentation)

---

## What's new in v4.0.0

V4 is a quality + layout + robustness batch built on top of V3's word-level
pipeline. Three passes (V4/V5/V6) together make the output production-grade.

### V4 — transcription quality pass
- **Beam search** — `BEAM_SIZE=5, BEST_OF=5` replaces greedy decoding; ~3-5×
  slower (still CPU/int8) but far fewer hallucinations.
- **Anti-hallucination** — `CONDITION_ON_PREVIOUS_TEXT=False` breaks cross-chunk
  echo loops; `REPETITION_PENALTY=1.1` suppresses degenerate repetition.
- **Hallucination filter** — `drop_hallucination_segments()` uses two-signal
  detection (word-collapse ≥50% **and** ≥3 shared consecutive words with a
  neighbour) to avoid false positives.
- **Fragment rejoin** — `merge_short_cues()` merges sub-second / ≤3-word orphans
  leftward while respecting sentence-final punctuation (不会跨句合并).
- **Cache fingerprint** — chunk cache names now include a sha1 of **all
  transcription recipe params** (`{base}.{fp}.chunk_N.json`); param changes
  auto-invalidate old caches.
- **Display window** — `--min-dur` (default 1.0s, library default 0) extends
  short-cue display time without touching alignment timestamps.

### V5 — output layout
- **Sub-folder output** — final 4 files land in `<outdir>/<base>/` with
  collision-based `_vN` version suffixes so 剪映 treats every re-run as a fresh
  import (no more stale cache).
- `--flat` reverts to legacy flat layout; `--prune-old` keeps only the two
  newest versions in the sub-folder.

### V6 — Baldwin 《天国王朝》 reported-issue fixes
- **B4 drift-snap** — `snap_drifted_words()` detects DTW word-timestamp drift
  (a single word landing seconds ahead of its sentence) and snaps it onto the
  sentence start before cue splitting; fixes the stray "可" orphan cue
  (12 → 10 segments on the real Baldwin clip).
- **B1' offset / tail** — `--offset` (default 0) and `--tail` (default 0.3s) on
  `generate`/`run` shift the **display window** so subtitles don't fire before
  the line is spoken; alignment timestamps are never touched.
- **B3 scene context** — `--source` + full-transcript injection + translation
  guidelines in the agent task (task version bumped to 2) so military-dialogue
  senses ("terms", "withdraw", "withdrawal") translate from whole-scene context.
- **B2 VAD tuning** — `VAD_THRESHOLD` lowered to 0.35 (was 0.5); `--vad-threshold`
  exposes it. Known trade-off: very short opening utterances may still be missed
  (Saladin case — manual cue recommended for the 剪映 import).

> Design invariant (unchanged since V1): **alignment timestamps are acoustic
> facts** produced by transcription and are never recomputed downstream —
> translation only rewrites text, and split/merge/gap only trim or cut at real
> word/silence boundaries. Display-only adjustments (`offset`, `tail`, `min_dur`)
> never touch alignment.

---

## What's new since V6 (V7–V13 hardening)

These close the defect classes found on the Jamie Foxx fan-edit (2026-08-10). As of
commit `ea77a83`, they are the **default** pipeline, not optional tuning.

- **V7 — quiet / music-heavy video procedure:** probe level, normalize, run VAD-off;
  regenerate discipline (`_vN` bump, never `rm -rf` the output subfolder).
- **V8 — mixed-language audio:** auto-detect is asymmetric; `language="ja"` over
  English triggers X→ja translation, not transliteration. Default `auto`; only
  `resegment --lang` when you *know* the source is mixed.
- **V9 — language decision:** default `auto`, no per-sentence detection; auto→en over
  mixed audio is the *invisible* failure, so `auto` beats forcing `en`.
- **V10 — big-segment drop fix:** `NO_SPEECH_THRESHOLD = 0.0` + temperature fallback.
  Two silence gates (VAD + Whisper internal) were dropping low-SNR speech. VAD is now
  opt-in (off by default), so the default run is already VAD-off; add `--vad` only for
  clean studio audio. Content type, not length, decides.
- **V11 — `fill_gaps.py`:** recovers inter-segment holes (≥2 s by default), in-segment collapse
  (`cps` vs median), prefix-collapse multi-pad probe (`_PROBE_PADS`), and echo dedup
  (`difflib` ratio > 0.7). Audit iterates until no new holes.
- **V12 — `verify_align.py`:** catches zh/en index drift before render (agent skips a
  line → whole-batch shift, invisible because English track is unchanged). Length-profile
  Pearson correlation (main signal) + digit co-occurrence; wired into `generate`,
  `--no-align-check` to silence.
- **V13 — orchestration:** agent engine is decided *first*; proxy/Google probe only under
  `--engine google`. CLI flags `--vad` (opt-in) / `--no-audit` / `--no-align-check`;
  `doctor` stays preflight.

> **Project iron rules (cross-tool):** (1) `AGENTS.md` is the sole authority — read it
> first, don't substitute local memory. (2) Every commit must update `AGENTS.md` +
> `README.md` + relevant `docs/` — code-only commits are incomplete. (3) SDD + TDD always:
> spec/ADR first, `make test` green, golden tests guard byte-stability.

---

## Project structure

```
video-translate/
├── src/video_translate/       # 核心源码 (14 modules)
│   ├── cli.py                 —— CLI 入口 (run/transcribe/translate/generate/backfill/…)
│   ├── transcribe.py          —— faster-whisper 转写引擎 + VAD + 分块缓存
│   ├── merge.py               —— 片段合并/断句/幻觉过滤/漂移吸附（纯函数管线）
│   ├── translate.py           —— agent 翻译任务生成 + Google Translate 兜底
│   ├── generate.py            —— 双语句 → SRT/TXT 产物 (offset/tail 显示窗口)
│   ├── proxy.py               —— HTTP 代理自动探测/设置
│   ├── config.py              —— 配置解析 (TOML → env → CLI)
│   ├── glossary.py            —— 术语表加载与注入 persona
│   ├── models.py              —— 数据模型 (Segment 等)
│   ├── io_utils.py            —— JSON 读写/原子写入
│   ├── srt_utils.py           —— SRT 格式化
│   └── ffmpeg_utils.py        —— ffmpeg/ffprobe 调用
├── tests/                     # 183 条测试 (pytest, TDD+golden 回归)
├── docs/                      # SDD 文档体系
│   ├── specs/                 —— 特性规格 (00–16)
│   ├── adr/                   —— 架构决策记录 (001–010)
│   ├── design/                —— 原理级设计文档
│   └── golden/                —— Golden 回归固件 (git-ignored, 本地重建)
├── videos/                    # 视频 + 中间产物 (git-ignored)
├── AGENTS.md                  # Agent 执行协议
├── README.md                  # 项目文档 (中英双语)
├── Makefile                   # 开发快捷命令
├── pyproject.toml             # 打包/依赖/版本
└── .gitignore
```

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

V4 defaults: `INPUT` is positional; `--base` = video filename stem; `--outdir` =
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
| `VT_SOURCE`             | source (V6: film/scene background for translation persona) |
| `VT_FULL_TRANSCRIPT`    | full_transcript (V6: include full EN transcript in task, default true) |
| `VT_VAD_THRESHOLD`      | vad_threshold (V4: Silero VAD threshold, default 0.35) |
| `HF_HOME`               | hf_cache_dir         |
| `HTTPS_PROXY`/`HTTP_PROXY` | proxy (fallback if `VT_PROXY` unset) |

### V4 CLI flags

| Flag (subcommand)            | Default | Meaning                                                        |
|------------------------------|---------|----------------------------------------------------------------|
| `--no-split` (`transcribe`/`run`) | off | disable cue splitting after merge (keep merged cues as-is) |
| `--merge-max-chars N` (`transcribe`/`run`) | 42 | max chars per cue before splitting (剪映 width) |
| `--gap N` (`generate`/`run`) | 0.2 | min gap (s) between cues; trims trailing silence, no overlap |
| `--glossary PATH` (`translate`/`run`) | — | glossary txt/json injected into the translation persona |
| `--offset N` (`generate`/`run`) | 0 | push display window start later (s), fix subtitle-ahead-of-speech (V6) |
| `--tail N` (`generate`/`run`) | 0.3 | extend display window end (s), prevent premature fade (V6) |
| `--min-dur N` (`generate`/`run`) | 1.0 | minimum cue display duration (s); 0 to disable (V4) |
| `--source TEXT` (`translate`/`run`) | — | film/scene background injected into translation persona (V6) |
| `--vad-threshold N` (`transcribe`/`run`) | 0.35 | Silero VAD sensitivity; lower = more speech detected (V4) |
| `--no-drift-snap` (`transcribe`/`run`) | off | disable DTW word-drift snapping before cue splitting (V6) |
| `--flat` (`generate`/`run`) | off | legacy flat output layout (no sub-folder, no _vN suffix) (V5) |
| `--prune-old` (`generate`/`run`) | off | keep only 2 newest versions in the sub-folder (V5) |
| `--strict` (`doctor`) | off | return exit code 7 if any check (incl. Google endpoint) fails |
| `--vad` (`transcribe`/`run`) | off | opt-in Silero VAD; default run is VAD-off (bare), which fixes VAD drop on music-heavy/low-SNR audio (V10) |
| `--no-audit` (`transcribe`/`run`) | off | skip the fill_gaps hole audit (V11) |
| `--no-align-check` (`generate`) | off | skip the zh/en index-drift guard (V12) |

### Outputs (V5: sub-folder with version suffix)

All 4 final files land in `<outdir>/<base>/` with collision-based `_vN` version
suffixes. The first run produces `<base>.*`; subsequent runs produce
`<base>_v1.*`, `<base>_v2.*`, etc., so 剪映 always imports a fresh copy.
`--flat` reverts to the legacy flat layout.

| File                       | Use                                  |
|----------------------------|--------------------------------------|
| `<base>[_vN].bilingual.srt`| 中文在上 / 英文在下，导入剪映         |
| `<base>[_vN].zh.srt`       | 纯中文字幕                            |
| `<base>[_vN].en.srt`       | 纯英文字幕                            |
| `<base>[_vN].txt`          | 双语校对稿                            |

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
  (architecture, word-level alignment, the cue-splitting mechanism, V1→V2→V3→V4 evolution).
- **Tests**: `make test` (fast unit+contract+golden, skips `@slow`); `make test-all`
  (includes the real e2e over the source video). Golden layers:
  `test_generate_golden` (build_outputs byte-exact), `test_merge_golden`
  (merge_segments determinism), `test_v1_golden_preserved` (V1 archived as `.v1`),
  `test_v2_golden_preserved` (V2 archived as `.v2`).

```bash
make test        # ~183 fast tests
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
- **Transcription quality** (V4) — beam search (`BEAM_SIZE=5, BEST_OF=5`) replaces
  greedy; `CONDITION_ON_PREVIOUS_TEXT=False` breaks cross-chunk echo; `REPETITION_PENALTY`
  suppresses loops; hallucination filter uses two-signal detection to avoid false
  positives.
- **Output layout** (V5) — final files go to a versioned sub-folder
  (`<base>[_vN].*`) so 剪映 cache collisions are impossible; `--flat` / `--prune-old`
  control the behaviour.
- **Drift snap** (V6) — `snap_drifted_words()` detects DTW word-timestamp drift
  (single words landing seconds ahead of their sentence) and snaps them before
  cue splitting (`--no-drift-snap` to disable).
- **Display window** (V6) — `--offset` / `--tail` shift the display window without
  touching alignment timestamps, fixing subtitle-ahead-of-speech.
- **Scene-context translation** (V6) — `--source` injects film/scene background;
  the agent task ships a full English transcript so the LLM translates with
  whole-scene awareness (military-dialogue senses like "terms"/"withdraw").

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

### V6 之后新增（V7–V13 加固）

以下修复来自 Jamie Foxx 粉丝混剪（2026-08-10）实战中暴露的缺陷类。自提交 `ea77a83` 起，它们已是**默认管线**，不是可选调参。

- **V7 — 安静 / 音乐重视频处理流程**：探电平、归一化、关 VAD 裸跑；重生成纪律（`_vN` 自动递增，绝不 `rm -rf` 输出子文件夹）。
- **V8 — 混合语种音频**：auto 检测不对称；英文上压 `language="ja"` 会触发 X→ja 翻译而非音译。默认 `auto`；仅当你**确定**源含混合语种时才用 `resegment --lang`。
- **V9 — 语种决策**：默认 `auto`，不做逐句检测；auto→en 压混合音频是"静默失败"，所以 `auto` 比强制 `en` 更安全。
- **V10 — 大段漏字修复**：`NO_SPEECH_THRESHOLD = 0.0` + 温度回退。两道静音闸门（VAD + Whisper 内部）曾丢弃低信噪比语音。VAD 改为选开（默认关），所以默认跑就是关 VAD 裸跑；仅干净录音才显式加 `--vad`。决定因素是内容类型，不是视频长度。
- **V11 — `fill_gaps.py`**：回收段间空洞（默认 ≥2 s）、段内塌陷（cps 低于文件自身中位数比例）、prefix-collapse 多 pad 探针（`_PROBE_PADS`）、回声去重（`difflib` ratio > 0.7）。审计多轮迭代直到无新洞。详见 [Spec 16](docs/specs/16-fill-gaps.md)。
- **V12 — `verify_align.py`**：渲染前捕获 zh/en 索引漂移（agent 漏翻一行 → 整批平移，因英文轨不变而静默不可见）。长度剖面 Pearson 相关（主信号）+ 数字共现；已接入 `generate`，`--no-align-check` 可关。详见 [Spec 17](docs/specs/17-verify-align.md)。
- **V13 — 编排**：先决定 agent 引擎；仅 `--engine google` 时才探代理 / Google 可达性。CLI 旗标 `--vad`（选开）/ `--no-audit` / `--no-align-check`；`doctor` 仍是预检。

> **项目铁律（跨工具）**：(1) `AGENTS.md` 是唯一权威——先读它，别用各自 memory 替代。(2) 每次提交必须同步 `AGENTS.md` + `README.md` + 相关 `docs/`——只改代码不更文档 = 未完成。(3) 始终 SDD + TDD：spec/ADR 先行，`make test` 必绿，golden 测试守卫字节稳定。

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

V4 默认值：`INPUT` 是位置参数；`--base` = 视频文件名主干；`--outdir` = 视频所在目录；`--lang` 自动检测；`--proxy` 自动检测（`--no-proxy` 走直连）。**断行（cue split）默认开启**（`--no-split` 可关）；`--gap` 默认 **0.2s**；`--tail` 默认 **0.3s**；`--min-dur` 默认 **1.0s**。默认的 **agent 引擎**在转写 + merge + 断行后停下，对外吐一份翻译任务文件给调用方 Agent（**退出码 6**）；想全自动（质量较低）跑则用 `--engine google`。各阶段也可用 `transcribe` / `translate` / `generate` 单独执行，或用 `run --skip transcribe` 续跑上次中断的部分。

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
| `VT_SOURCE`               | source（V6：影片/场景背景描述，注入翻译 persona） |
| `VT_FULL_TRANSCRIPT`      | full_transcript（V6：翻译任务是否附全文上下文，默认 true） |
| `VT_VAD_THRESHOLD`        | vad_threshold（V4：Silero VAD 阈值，默认 0.35） |
| `HF_HOME`                 | hf_cache_dir         |
| `HTTPS_PROXY`/`HTTP_PROXY` | proxy（在 `VT_PROXY` 未设时兜底） |

### V4 新增 CLI 参数

| 参数（子命令）               | 默认  | 含义                                                |
|------------------------------|-------|-----------------------------------------------------|
| `--no-split`（`transcribe`/`run`） | 关 | 关闭 merge 后的断行（保留合并后的整段）           |
| `--merge-max-chars N`（`transcribe`/`run`） | 42 | 每行最大字符数（剪映宽度），超过即断行        |
| `--gap N`（`generate`/`run`） | 0.2 | 相邻 cue 最小间隔（秒）；裁掉尾随静音、不重叠    |
| `--glossary PATH`（`translate`/`run`） | — | 术语表（txt/json），注入翻译 persona            |
| `--offset N`（`generate`/`run`） | 0 | 显示窗口起点后移（秒），解决"字幕比声音早出"（V6） |
| `--tail N`（`generate`/`run`） | 0.3 | 显示窗口尾端延长（秒），防止字幕过早消失（V6） |
| `--min-dur N`（`generate`/`run`） | 1.0 | 字幕最短显示时长（秒）；设为 0 关闭（V4） |
| `--source TEXT`（`translate`/`run`） | — | 影片/场景背景描述，注入翻译 persona（V6） |
| `--vad-threshold N`（`transcribe`/`run`） | 0.35 | Silero VAD 灵敏度；越低检出越多语音（V4） |
| `--no-drift-snap`（`transcribe`/`run`） | 关 | 关闭 DTW 词级漂移吸附，保留原始断句（V6） |
| `--flat`（`generate`/`run`） | 关 | 退回扁平旧布局（无子文件夹、无 _vN 后缀）（V5） |
| `--prune-old`（`generate`/`run`） | 关 | 子文件夹内仅保留最新两份版本（V5） |
| `--strict`（`doctor`） | 关 | 任一检查（含 Google 端点）失败则返回退出码 7     |
| `--vad`（`transcribe`/`run`） | 关 | Silero VAD 改为「选开」：默认即裸跑（关 VAD），正好是修复音乐重/低信噪比音频漏切的方案（V10）；干净录音可显式加 `--vad` 开启 |
| `--no-audit`（`transcribe`/`run`） | 关 | 跳过 fill_gaps 空洞审计（V11） |
| `--no-align-check`（`generate`） | 关 | 跳过 zh/en 索引漂移护栏（V12） |

### 产物（V5：子文件夹 + 版本后缀）

所有 4 个最终文件写入 `<outdir>/<base>/` 子文件夹，带碰撞式 `_vN` 版本后缀。
首次运行产出 `<base>.*`，后续重跑自动 bump 为 `<base>_v1.*`、`<base>_v2.*`……剪映每次当新文件导入，杜绝缓存错乱。`--flat` 退回扁平旧布局。

| 文件                       | 用途                                 |
|----------------------------|--------------------------------------|
| `<base>[_vN].bilingual.srt`| 中文在上 / 英文在下，导入剪映         |
| `<base>[_vN].zh.srt`       | 纯中文字幕                            |
| `<base>[_vN].en.srt`       | 纯英文字幕                            |
| `<base>[_vN].txt`          | 双语校对稿                            |

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
- **设计文档**：[`docs/design/`](docs/design) 是原理级说明（架构、词级对齐、断句整体机制、V1→V2→V3→V4 演进）。
- **测试**：`make test`（快速单测 + 契约 + golden，跳过 `@slow`）；`make test-all`（含基于源视频的真实 e2e）。golden 分层：`test_generate_golden`（build_outputs 字节级一致）、`test_merge_golden`（merge_segments 确定性）、`test_v1_golden_preserved`（V1 归档为 `.v1`）、`test_v2_golden_preserved`（V2 归档为 `.v2`）。

```bash
make test        # 约 183 个快测
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
- **转写质量**（V4）— 束搜索（`BEAM_SIZE=5, BEST_OF=5`）替代贪婪解码；`CONDITION_ON_PREVIOUS_TEXT=False` 截断跨段复读；`REPETITION_PENALTY` 抑制循环；幻觉过滤器用双信号检测防误删。
- **输出布局**（V5）— 最终文件进版本化子文件夹（`<base>[_vN].*`），剪映缓存冲突从根上消除；`--flat`/`--prune-old` 控制行为。
- **漂移吸附**（V6）— `snap_drifted_words()` 检测 DTW 词级时间戳漂移（单个词比所在句子早数秒），在断句前吸附回去（`--no-drift-snap` 可关）。
- **显示窗口**（V6）— `--offset`/`--tail` 平移显示窗口而不触碰对齐时间戳，修复"字幕比声音早出"。
- **场景上下文翻译**（V6）— `--source` 注入影片/场景背景；agent 任务附带完整英文全文，LLM 基于全场景理解翻译（军事对话中 "terms"/"withdraw" 等准确还原）。

## License

Released under the **MIT License** — see [LICENSE](LICENSE) for the full text.

Copyright (c) 2026 BruceYang

## 许可证

本项目基于 **MIT 许可证** 发布，完整文本见 [LICENSE](LICENSE)。

版权所有 (c) 2026 BruceYang
