# Spec 00 — Overview & Data Flow

## Purpose
`video-translate` turns a video into bilingual (Chinese/English) subtitles that
import cleanly into Jianying (剪映). It is a faithful, testable migration of a
previously validated ad-hoc pipeline.

## Design invariant (the one rule everything serves)
> **Timestamps are acoustic facts, never recomputed.**
> faster-whisper's `start`/`end` are carried unchanged through the entire
> pipeline. Translation only rewrites text. This guarantees audio/subtitle
> alignment "for free" — the property the user validated as near-perfect.

## Three-stage pipeline
```
video (mp4/…)
   │  stage 1: transcribe  (faster-whisper large-v3, CPU/int8, chunked+resumable)
   ▼
{base}.segments_en.json         list[{start, end, text}]
   │  stage 2: translate   (Google Translate via HTTP proxy, incremental)
   ▼
{base}.zh_segments.json         {str_index: zh_text}
   │  stage 3: generate    (pure function, byte-stable)
   ▼
{base}.bilingual.srt   {base}.zh.srt   {base}.en.srt   {base}.txt
```

Intermediate per-chunk artifacts: `chunk_0.json`, `chunk_1.json`, … (stage 1
resume state). Failure artifact: `{base}.agent_pending.json` (segments Google
could not translate, for agent backfill).

## Stage responsibilities
| Stage | Module | Deterministic? | Golden-testable? |
|---|---|---|---|
| transcribe | `transcribe.py` + `ffmpeg_utils.py` | No (model, timing) | Contract only |
| translate | `translate.py` + `proxy.py` | No (network) | Contract only |
| generate | `generate.py` + `srt_utils.py` | **Yes (pure)** | **Byte-exact** |

## Scope
- **v1 (locked at tag `v1.0.0`)**: faithful migration of transcribe → translate →
  generate; CLI; tests; docs. Golden archived as `docs/golden/apollo_story.v1.*`.
- **v2 (tag `v2.0.0`)**: segment-merge layer (Spec 08); agent-as-engine
  translation (Spec 09, default `--engine agent`, no LLM client dep); `backfill`
  subcommand (Spec 10); CLI/UX overhaul — zero-config positional input, auto
  base/outdir/lang/proxy (Spec 11, ADR-006/007). V1 invariants preserved:
  timestamps never recomputed, SOCKS rejected, CPU/int8 forced.

## Related specs
- Segment schema: `01-segment-schema.md`
- Per-stage detail: `02-transcribe.md`, `03-translate.md`, `04-generate-srt.md`
- CLI: `11-cli-v2.md` · Config: `06-config.md` · Gotchas: `07-gotchas.md`
