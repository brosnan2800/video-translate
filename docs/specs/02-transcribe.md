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

## Decoding parameters

> 原节标题为「Forced parameters (**not configurable**)」—— 该口径**已被后续决策取代**：
> `device` / `compute_type` 改为 `auto` 且可覆盖（[ADR-014](../adr/014-cuda-device-abstraction.md)）、
> VAD 默认**裸跑**（[ADR-011](../adr/011-vad-opt-in.md) / [ADR-034](../adr/034-audio-routing-redesign.md)）、
> beam / best_of 提升为 5（V4 反幻觉）。下表为**现状**。

| Param | Value | Rationale |
|---|---|---|
| device | `auto`（可经 `--device` / `VT_DEVICE` / `[transcribe]` 覆盖） | ADR-014：有 CUDA 用 CUDA，否则回落 cpu |
| compute_type | `auto`（同上） | 同上；无 CUDA 时解析为 int8 |
| beam_size | `5` | 反幻觉标准设置；比 greedy 慢约 3-5x（V4） |
| best_of | `5` | 与 beam 配套 |
| condition_on_previous_text | `False` | 跨段上下文会让幻觉自我强化（V4） |
| repetition_penalty | `1.1` | 段内轻度重复惩罚（V4） |
| vad_filter | `use_vad`（**默认 `False`**） | ADR-011 / ADR-034：默认裸跑，保留笑声/欢呼下的真音 |
| vad_parameters | `min_silence_duration_ms=500, speech_pad_ms=200` | **仅 `--vad` 时生效** |
| word_timestamps | `True` | 词级时间戳（Spec 12）：切分与补洞的依据 |
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
