# video-translate

> 📖 中文文档：[README.zh.md](README.zh.md)

Turn a video into **Jianying(剪映)-importable bilingual (zh/en) subtitles** with a
faithful, resumable pipeline:

```
video ──transcribe──▶ segments_en.json ──translate──▶ zh_segments.json ──generate──▶ *.srt / *.txt
        (faster-whisper                 (Google Translate               (byte-stable
         large-v3, CPU/int8)             via HTTP proxy)                 SRT/TXT)
```

Design invariant: **timestamps are acoustic facts** produced by transcription and
are never recomputed downstream — translation only rewrites text, so subtitles
stay glued to the audio.

---

## For AI agents / automation

If you are an agent (WorkBuddy / Claude Code / Cursor / Cline / …) asked to
generate subtitles with this project, **read [`AGENTS.md`](AGENTS.md) first** and
follow its execution protocol (Preflight → run → verify). Do not improvise the
pipeline; the guide encodes the resume, proxy, and golden-verification rules.

## For WorkBuddy users (skill)

A thin WorkBuddy skill can wrap this CLI. To install it, copy the skill folder
into `~/.workbuddy/skills/video-translate/` (user-level) or
`{workspace}/.workbuddy/skills/` (project-level), then invoke it in chat. The
skill delegates all real work to the `video-translate` CLI documented here.

---

## Quick start (humans)

```bash
# 1. Install (creates .venv, installs deps + this package editable)
make install-dev

# 2. Check your environment (ffmpeg, model cache, proxy, deps, engine)
make doctor

# 3. Download the large-v3 model once (~3GB, reused across projects)
.venv/bin/video-translate setup            # reuses ~/.cache/huggingface if present

# 4. Full pipeline — zero config (base/outdir default from the video path)
.venv/bin/video-translate run "videos/apollo.mp4"             # agent engine (default)
.venv/bin/video-translate run "videos/apollo.mp4" --engine google   # headless end-to-end

# 5. Import <video_dir>/apollo.bilingual.srt into 剪映
```

V2 defaults: `INPUT` is positional; `--base` = video filename stem; `--outdir` =
the video's own directory; `--lang` auto-detects; `--proxy` auto-detects
(`--no-proxy` for direct). The **agent engine** (default) stops after transcribe +
merge and emits a translation task for the calling agent (exit 6); use
`--engine google` for a fully automatic (lower-quality) run. Run stages
individually with `transcribe` / `translate` / `generate`, or resume a partial run
with `run --skip transcribe`.

## Requirements

- **Python 3.13** (`.python-version` pins it).
- **ffmpeg + ffprobe** on `PATH`.
- **HTTP proxy** for model download & Google Translate (default
  `http://127.0.0.1:7890`, e.g. Clash). **SOCKS is rejected** — it breaks
  huggingface_hub (see [ADR-003](docs/adr/003-http-proxy-only.md)).
- ~3GB disk for the large-v3 model (shared HF cache at `~/.cache/huggingface`).

## Configuration

Priority: **CLI args > env vars > `.video-translate.toml` > defaults**. See
[Spec 06](docs/specs/06-config.md). Example `.video-translate.toml`:

```toml
[transcribe]
model = "large-v3"
chunk = 240.0
lang  = "auto"          # auto-detect (default)

[translate]
tgt = "zh-CN"

[llm]
persona = "你是一位资深中英字幕译者。遵循「信达雅」+ 口语感……"

[merge]
merge_enabled   = true
merge_max_dur   = 8.0
merge_max_gap   = 0.5
```

## Outputs

| File                       | Use                                  |
|----------------------------|--------------------------------------|
| `<base>.bilingual.srt`     | 中文在上 / 英文在下，导入剪映         |
| `<base>.zh.srt`            | 纯中文字幕                            |
| `<base>.en.srt`            | 纯英文字幕                            |
| `<base>.txt`               | 双语校对稿                            |

## Development (TDD + SDD)

- **Specs first**: [`docs/specs/`](docs/specs) (00–11) define behavior before code.
- **Decisions**: [`docs/adr/`](docs/adr) records why (CPU/int8, chunked-resume,
  HTTP-proxy-only, segment-merge, agent-as-engine, lang-autodetect, proxy-autodetect).
- **Tests**: `make test` (fast unit+contract+golden, skips `@slow`); `make test-all`
  (includes the real e2e over the source video). Golden layers:
  `test_generate_golden` (build_outputs byte-exact), `test_merge_golden`
  (merge_segments determinism), `test_v1_golden_preserved` (V1 archived as `.v1`).

```bash
make test        # ~105 fast tests
make test-all    # + slow e2e (needs model + video)
make clean
```

## Design notes

- **Agent as engine** (V2, [ADR-005](docs/adr/005-agent-as-engine.md)) — the CLI's
  default `--engine agent` emits a translation task for the calling agent (which
  has its own LLM); no LLM client dependency. Google is the `--engine google`
  headless fallback.
- **Segment merge** (V2, [ADR-004](docs/adr/004-segment-merge-strategy.md)) —
  adjacent Whisper fragments rejoin into readable cues; timestamps taken verbatim
  (first start / last end), never recomputed. Default ON (`--no-merge` to skip).
- **Resumable transcription** — audio split into `chunk` chunks; each
  `chunk_N.json` is persisted atomically and skipped on re-run
  ([ADR-002](docs/adr/002-chunked-resume.md)).
- **CPU / int8** — CTranslate2 has no AMD/Metal support; forced
  ([ADR-001](docs/adr/001-cpu-int8.md)).
- **Proxy auto-detect** (V2, [ADR-007](docs/adr/007-proxy-autodetect.md)) —
  `--no-proxy` / `--proxy` / env / probe 7890 → direct. SOCKS still rejected
  ([ADR-003](docs/adr/003-http-proxy-only.md)).

## License

Private project. See repository owner.
