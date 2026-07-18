# video-translate

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

# 2. Check your environment (ffmpeg, model cache, proxy, deps)
make doctor

# 3. Download the large-v3 model once (~3GB, reused across projects)
.venv/bin/video-translate setup            # reuses ~/.cache/huggingface if present

# 4. Full pipeline on a video
.venv/bin/video-translate run \
    --input "videos/steveharvy-the apollo story.mp4" \
    --outdir outputs --base apollo_story

# 5. Import outputs/apollo_story.bilingual.srt into 剪映
```

Run stages individually with `transcribe` / `translate` / `generate`, or resume a
partial run with `run --skip transcribe`.

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
lang  = "en"

[translate]
proxy = "http://127.0.0.1:7890"
tgt   = "zh-CN"
```

## Outputs

| File                       | Use                                  |
|----------------------------|--------------------------------------|
| `<base>.bilingual.srt`     | 中文在上 / 英文在下，导入剪映         |
| `<base>.zh.srt`            | 纯中文字幕                            |
| `<base>.en.srt`            | 纯英文字幕                            |
| `<base>.txt`               | 双语校对稿                            |

## Development (TDD + SDD)

- **Specs first**: [`docs/specs/`](docs/specs) (00–07) define behavior before code.
- **Decisions**: [`docs/adr/`](docs/adr) records why (CPU/int8, chunked-resume,
  HTTP-proxy-only).
- **Tests**: `make test` (fast unit+contract, skips `@slow`); `make test-all`
  (includes the real e2e over the source video). The **golden regression**
  (`tests/test_generate_golden.py`) guarantees byte-exact output vs the validated
  `docs/golden/apollo_story.*` baseline.

```bash
make test        # 49 fast tests
make test-all    # + slow e2e (needs model + video)
make clean
```

## Design notes

- **Resumable transcription** — audio split into `chunk` chunks; each `chunk_N.json`
  is persisted atomically and skipped on re-run ([ADR-002](docs/adr/002-chunked-resume.md)).
- **CPU / int8** — CTranslate2 has no AMD/Metal support; forced, not configurable
  ([ADR-001](docs/adr/001-cpu-int8.md)).
- **Google primary, agent fallback** — segments Google can't translate go to
  `<base>.agent_pending.json` for an agent to backfill (Spec 03).

## License

Private project. See repository owner.
