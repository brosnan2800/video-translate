# Coding instructions

Applies to all source under `src/video_translate/`.

## Principles
- **SDD**: behavior is defined in `docs/specs/` before code changes. If you change
  behavior, update the matching spec in the same PR.
- **Faithful migration**: this project was migrated from validated scripts. The
  golden baseline (`docs/golden/`) is the source of truth for output. Do not
  "improve" formatting without deliberately updating golden + spec.
- **Pure core, thin edges**: keep transformation logic pure and testable
  (`srt_utils`, `generate`, `models`, `plan_chunks`, `merge_chunks`). Push I/O,
  network, and subprocess to the edges (`io_utils`, `proxy`, `ffmpeg_utils`).

## Conventions
- Python 3.13, `from __future__ import annotations`, type hints on public funcs.
- ASCII straight quotes in code. `ensure_ascii=False` for JSON; UTF-8 everywhere.
- **Lazy-import heavy deps** (`faster_whisper`, `deep_translator`) inside the
  functions that need them, so unit tests and `doctor` run without them.
- **Atomic writes** for any file that participates in resume (temp + `os.replace`).
- Forced constants (`DEVICE=cpu`, `COMPUTE_TYPE=int8`, `BEAM_SIZE=1`, VAD params)
  live in `transcribe.py` and are NOT configurable — see ADR-001.
- Injectable seams: `translate_fn`, `progress`, and command builders exist so the
  pipeline is testable without network/model. Preserve them.

## Do not
- Recompute timestamps anywhere after transcription.
- Add a SOCKS proxy path.
- Introduce a user-level config layer (project-level `.video-translate.toml` only).
