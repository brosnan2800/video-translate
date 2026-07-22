# Spec 05 — CLI

Module: `cli.py`. Entry points: `video-translate` (console script) and
`python -m video_translate`.

## Subcommands
| Command      | Purpose                                                    |
|--------------|------------------------------------------------------------|
| `transcribe` | video → `{base}.segments_en.json` (chunked, resumable, +merge) |
| `translate`  | `segments_en.json` → task (agent) or `zh_segments.json` (google) |
| `generate`   | segments + zh → four subtitle files                        |
| `run`        | full pipeline (agent stops at exit 6; google end-to-end)   |
| `backfill`   | backfill `agent_pending.json` via the agent engine         |
| `setup`      | check/download HF model (reuse if cached)                  |
| `doctor`     | environment self-check (never fails hard)                  |

## Arguments (V2 defaults)
- `transcribe`/`run`: **`INPUT` positional**; `--outdir` (default: video's dir),
  `--base` (default: video stem), `--lang` (default: auto-detect), `--model`,
  `--chunk`, `--threads`, `--proxy`, `--no-proxy`, `--no-merge`. `run` also takes
  `--engine {agent,google}` (default `agent`), `--skip`, `--src`, `--tgt`.
- `translate`: `--segments` `--out` (required); `--engine {agent,google}`
  (default `agent`), `--pending`, `--proxy`, `--no-proxy`, `--src`, `--tgt`.
- `generate`: `--segments` `--zh` `--outdir` (required); `--base` (default: derived).
- `backfill`: `--pending` `--out` (required); `--segments`, `--outdir`, `--base`,
  `--agent-zh` (triggers merge + generate).
- `setup`: `--model`, `--proxy`, `--no-proxy`.

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
| 6    | **awaiting agent action** — transcribe+task done; agent must translate (V2) |

## Contract (testable without heavy deps)
- `build_parser()` accepts every documented flag and rejects unknown subcommands
  with exit 2.
- `main(["doctor"])` returns 0 and prints device=cpu / compute_type=int8.
- `main(["generate", ...])` on golden inputs returns 0 and writes four files.
- ffmpeg-dependent commands return 3 when ffmpeg/ffprobe are absent (lazy check
  before any model import).
