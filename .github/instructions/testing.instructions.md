# Testing instructions

TDD: write/adjust a failing test before the implementation change; keep the suite
green.

## Layout
- Fast tests (default): unit + contract, no network, no model, no ffmpeg binary.
  Run via `make test` (pytest `addopts = -m 'not slow'`).
- Slow tests: real end-to-end over the source video, marked `@pytest.mark.slow`.
  Run via `make test-all` on a machine with faster-whisper + large-v3 + the video.

## What each test guards
- `test_generate_golden.py` — **byte-exact** reproduction of `docs/golden/*`. This
  is the load-bearing regression; if it fails, output drifted.
- `test_transcribe_contract.py` — chunk planning math + **resume** (pre-seeded
  `chunk_N.json` → model never loads, extract never called).
- `test_translate_contract.py` — resume/skip, checkpointing, pending-on-failure,
  via an injected `translate_fn` (no network).
- `test_proxy.py` — HTTP vars forced, SOCKS popped/rejected (ADR-003).
- `test_config.py` — CLI > env > toml > default priority.
- `test_cli_smoke.py` — argparse, exit codes, `generate` via CLI byte-exact.
- `test_io_utils.py` / `test_srt_utils.py` / `test_models.py` — pure helpers,
  including the `ms==1000` carry edge case.
- `test_pipeline_idempotent.py` — determinism + timestamps-not-recomputed.

## Rules
- No test may require network or the 3GB model except those marked `@slow`.
- Prefer injecting seams (`translate_fn`, `monkeypatch` of `probe_duration`/
  `extract_chunk`) over touching real resources.
- Current baseline: **49 fast tests must pass** before commit.
