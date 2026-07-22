# Spec 11 — CLI v2 (zero-config + engine selection)

Module: `cli.py`. Supersedes the V1 CLI surface in `05-cli.md` (which is updated
in Stage 4). Implements the CLI/UX overhaul (decision A1–A4).

## Zero-config happy path
```
video-translate run video.mp4                 # agent engine (default)
video-translate run video.mp4 --engine google # headless end-to-end
video-translate transcribe video.mp4          # stage-only
```
- `INPUT` is a **positional** argument (was `--input` in V1).
- `--outdir` defaults to the video's own directory (`Path(INPUT).parent`).
- `--base` defaults to the video filename stem (`Path(INPUT).stem`); was hardcoded
  `"apollo_story"` in V1 (a bug — any other video was misnamed).
- `--lang` defaults to `None` = Whisper auto-detect (was hardcoded `"en"`).

## Engine selection (`--engine {agent,google}`, default `agent`)
- **agent** (default, decision 0): the CLI does NOT call any LLM API. After
  transcribe it emits `<base>.translate_task.json` (see `09-agent-translate.md`)
  and returns **exit 6** (`EXIT_AWAITING_AGENT`) with a `[AWAITING_AGENT]` stdout
  marker + copy-paste `generate` instructions. The calling agent (WorkBuddy /
  Claude Code / …) translates using its own LLM and writes `zh_segments.json`.
- **google**: V1 path — `translate_segments` via `deep_translator` (headless
  fallback). Runs end-to-end in `run`.

## Proxy auto-detection (`--no-proxy` / `--proxy`)
`cli._resolve_proxy(args)` → `proxy.detect_proxy(...)`:
`--no-proxy` → `--proxy` → `VT_PROXY` → `HTTPS_PROXY`/`HTTP_PROXY` → TCP probe
127.0.0.1:7890 → `None` (direct). `setup_http_proxy(None)` clears HTTP env vars
(direct); `setup_http_proxy("<http>")` forces them. SOCKS still rejected (ADR-003).

Deviation from plan: detect_proxy returns `None` (direct) on probe failure
rather than raising — direct egress often works and raising would break
local-only transcription (model cached, no network).

## `run` parser fix
V1 `run` parser omitted `--model/--chunk/--lang/--threads`, so `cmd_run`'s
`getattr(args, ...)` always fell back to defaults. V2 adds these flags so
`run` can override transcribe parameters.

## Exit codes
0 ok · 1 runtime · 2 args · 3 missing dep · 4 proxy · 5 killed · **6 awaiting
agent** (transcribe+task done; agent must translate). Exit 6 is new in V2
(breaking); documented in `05-cli.md` and `AGENTS.md`.

## `backfill` subcommand
Placeholder in Stage 1 (returns runtime "not yet implemented"); fully implemented
in Stage 3 (see `10-backfill.md`).
