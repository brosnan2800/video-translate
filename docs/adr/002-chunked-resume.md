# ADR-002 — Chunked, resumable transcription

- **Status**: Accepted
- **Date**: 2026-07-16
- **Context**: CPU/int8 transcription (ADR-001) of a long video takes many
  minutes. The original `transcribe_chunked.py` split audio into chunks BUT
  re-transcribed every chunk on each run — a crash or SIGKILL meant starting
  over. WorkBuddy's sandbox can OOM-kill long jobs, so resume is essential.

## Decision
Split audio into fixed-length chunks (`chunk=240s` default) via ffmpeg
(`-ar 16000 -ac 1` 16kHz mono WAV). Transcribe each chunk, persist
`chunk_N.json` immediately with an **atomic write** (temp file + `os.replace`).
On re-run, any chunk whose `chunk_N.json` already exists is **skipped**;
`merge_chunks` reassembles the full `segments_en.json` with globally-offset
timestamps.

`plan_chunks(total, chunk)` → `n_chunks = int(total // chunk) + 1`.

## Consequences
- **Positive**: True resume — interrupted jobs continue where they stopped; no
  duplicate compute; atomic writes survive mid-write kills; CLI exit code 5
  signals "killed but partially done, safe to re-run".
- **Negative**: A chunk boundary can split a spoken sentence. Acceptable for v1;
  v2's segment-merge pass will address cross-chunk sentence stitching.
- **Trade-off**: Extra on-disk `chunk_N.json` files; cleaned by `make clean`.
