# Spec 05 — CLI

Module: `cli.py`. Entry points: `video-translate` (console script) and
`python -m video_translate`.

## Subcommands
| Command      | Purpose                                                    |
|--------------|------------------------------------------------------------|
| `transcribe` | video → `{base}.segments_en.json` (chunked, resumable)     |
| `translate`  | `segments_en.json` → `{base}.zh_segments.json` (+ pending) |
| `generate`   | segments + zh → four subtitle files                        |
| `run`        | full pipeline: transcribe → translate → generate           |
| `setup`      | check/download HF model (reuse if cached)                  |
| `doctor`     | environment self-check (never fails hard)                  |

## Arguments (defaults)
- `transcribe`: `--input` `--outdir` (required); `--base apollo_story`,
  `--model large-v3`, `--chunk 240.0`, `--threads None`, `--lang en`.
- `translate`: `--segments` `--out` (required); `--pending None`, `--proxy None`,
  `--src en`, `--tgt zh-CN`.
- `generate`: `--segments` `--zh` `--outdir` (required); `--base apollo_story`.
- `run`: `--input` `--outdir` (required); `--base`, `--skip {transcribe,translate,
  generate}*`, `--proxy`, `--src`, `--tgt`. `--skip` lets a re-run reuse existing
  intermediate JSON (e.g. `--skip transcribe`).
- `setup`: `--model large-v3`, `--proxy None`.

Config resolution (Spec 06) fills any value left as CLI-`None`.

## Exit codes
| Code | Meaning                                                          |
|------|-----------------------------------------------------------------|
| 0    | success                                                         |
| 1    | runtime error                                                  |
| 2    | argument error (argparse)                                       |
| 3    | missing dependency (ffmpeg / HF model)                          |
| 4    | proxy error (SOCKS given — ADR-003)                             |
| 5    | transcription killed (SIGKILL); some chunks done, safe re-run  |

## Contract (testable without heavy deps)
- `build_parser()` accepts every documented flag and rejects unknown subcommands
  with exit 2.
- `main(["doctor"])` returns 0 and prints device=cpu / compute_type=int8.
- `main(["generate", ...])` on golden inputs returns 0 and writes four files.
- ffmpeg-dependent commands return 3 when ffmpeg/ffprobe are absent (lazy check
  before any model import).
