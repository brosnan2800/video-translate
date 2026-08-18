# Spec 00 — Overview & Data Flow

## Purpose
`video-translate` turns a video into bilingual (Chinese/English) subtitles that
import cleanly into Jianying (剪映). It is a faithful, testable migration of a
previously validated ad-hoc pipeline.

## Design invariant (the one rule everything serves)
> **Timestamps are carried unchanged, never recomputed** — but they are *not*
> acoustic facts.
> faster-whisper's `start`/`end` (word- and segment-level) are DTW *posterior
> estimates*: they drift and collapse, they are not ground truth. Therefore:
> - Segment boundaries are anchored to **VAD / silence** where possible
>   (content-type routing, ADR-011);
> - Alignment must be cross-checked against an **independent reference**
>   (`ffmpeg silencedetect` measured silence), never self-asserted by whisper;
> - True acoustic repair (forced alignment, 96% vs word-level 82%) depends on
>   WhisperX and is locked to GPU (MAJOR_VERSION_PLAN T3); the Mac path only
>   *detects + routes*, it does not *repair*.
> Subtitle correctness has three orthogonal layers — **acoustic / content /
> presentation** (Spec 18) — and must not be collapsed into one.
> See ADR-012.

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
- Hardening (V7–V13): `16-fill-gaps.md` (V11 coverage audit) · `17-verify-align.md` (V12 zh/en index-drift guard) · `18-verify.md` (unified self-check: acoustic/content/presentation lanes, ADR-012)
