# Spec 02 — Transcribe

Module: `transcribe.py` (+ `ffmpeg_utils.py`). Produces `{base}.segments_en.json`.

## Algorithm
1. `total = probe_duration(input)` via ffprobe.
2. `plan_chunks(total, chunk)` → `[(ci, cstart, cdur), ...]` where
   `n_chunks = int(total // chunk) + 1`, `cdur = min(chunk, total - cstart)`,
   chunks with `cdur <= 0` are dropped.
3. For each chunk `ci`:
   - **Resume**: if `chunk_{ci}.json` exists and is valid → reuse it, skip work.
   - Else: `extract_chunk` → 16kHz mono WAV; `WhisperModel.transcribe(...)`;
     offset each segment time by `cstart`; round to 2 decimals; save
     `chunk_{ci}.json`; delete the WAV.
4. Merge all chunk lists in order → `{base}.segments_en.json`.

## Forced parameters (not configurable)
| Param | Value | Rationale |
|---|---|---|
| device | `cpu` | CTranslate2 has no AMD/Metal GPU support (see ADR-001) |
| compute_type | `int8` | Fits CPU, acceptable quality |
| beam_size | `1` | Greedy; large-v3 quality loss negligible, ~1.5x faster |
| best_of | `1` | With greedy decoding |
| vad_filter | `True` | Drops silence → cleaner segmentation |
| vad_parameters | `min_silence_duration_ms=500, speech_pad_ms=200` | Tuned values |
| audio | `-ar 16000 -ac 1` | Whisper's expected input |

## Resume guarantee (improvement over original)
The original script re-extracted and re-transcribed every chunk on each run.
v1 skips any chunk whose `chunk_{ci}.json` already exists. The model is loaded
lazily — if all chunks are already done, no model load happens at all.

## Contract (testable without running Whisper — uses golden `chunk_0.json`)
- Each element has numeric `start`/`end` and string `text`.
- Within a chunk, timestamps are non-decreasing and `end >= start`.
- Merged `segments_en.json` last `end` ≤ media duration.
- `plan_chunks` covers `[0, total)` with no gap/overlap in planned windows.

## Confidence fields (ADR-020)
Each emitted segment **also carries** Whisper's per-segment confidence fields
when available, so the hallucination guard (Spec 12 / `merge.py`) can use them
without re-decoding:

- `avg_logprob` (float): mean token log-probability. Low ⇒ likely hallucination.
- `no_speech_prob` (float): Whisper's own no-speech score.
- `compression_ratio` (float): repetition/looping indicator.

These are copied verbatim from the faster-whisper `Segment` by `_seg_to_dict`;
if a field is absent (older model / timeout) it is **omitted**, not defaulted —
so the downstream fifth signal degrades inertly rather than false-firing.
