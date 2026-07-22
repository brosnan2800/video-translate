# Spec 08 — Segment merge

Module: `merge.py`. A pure post-processing stage between transcribe and
translate/generate. Default ON (`--no-merge` to skip). Decision B6.

## Problem
Whisper sometimes splits a single sentence across multiple cues (mid-sentence
fragments with no terminal punctuation). These read badly as subtitles.

## Algorithm (`merge_segments`)
Greedily merge segment `i` into the current group when ALL hold:
1. `gap = seg[i].start - group.end < max_gap` (default 0.5s)
2. `(seg[i].end - group.start) <= max_dur` (default 8.0s)
3. `respect_sentence_end` → group's last text does NOT end with `[.!?]`
4. `seg[i].start >= group.end` (no overlap; chunks are monotonic)

Each merged seg = `{start: group[0].start, end: group[-1].end,
text: " ".join(texts)}`.

## Invariant (load-bearing)
**Timestamps are never recomputed.** Merged `start`/`end` are real acoustic
boundies taken verbatim from the input (first start / last end). Enforced by
`test_all_boundary_values_come_from_input`, `test_merged_start_is_first_*`,
`test_merged_end_is_last_*`, and the byte-exact golden.

## `max_chars` deviation (important)
`DEFAULT_MAX_CHARS = 42` (剪映 single-line limit, from WhisperX) is **reserved
for a future v3 split pass** and is NOT a merge gate. Reason: splitting a too-long
cue requires word-level timestamps to redivide the time, which V2 does not have.
Using max_chars to *block* merges would prevent mid-sentence fragments from
rejoining into sentences (apollo: 209→209 no-op). `max_dur` already bounds merged
length indirectly (a sentence spoken in ≤8s is bounded). Splitting deferred to v3.

## File naming
- transcribe writes `<base>.segments_en.json` (raw, V1 behavior).
- `apply_merge` copies it to `<base>.segments_raw.json`, then overwrites
  `<base>.segments_en.json` with the merged result.
- `--no-merge`: skip; `segments_en.json` stays raw, no `segments_raw.json`.
- Downstream (translate/generate/agent-task) always read `segments_en.json` —
  merge is transparent to them.

## Golden
- `docs/golden/apollo_story.segments_raw.json` — V1 unmerged (209 segs).
- `docs/golden/apollo_story.merged_segments.json` — frozen `merge_segments(raw)`
  (199 segs). `test_merge_golden.py` asserts byte-exact determinism.
- `docs/golden/apollo_story.segments_en.json` stays V1-unmerged until Stage 4
  (where it becomes the merged canonical + retranslated zh).

## Defaults
`max_dur=8.0`, `max_gap=0.5`, `respect_sentence_end=True`. Configurable via toml
`[merge]` / env `VT_MERGE_MAX_DUR` / `VT_MERGE_MAX_GAP`. `merge_max_chars` is
loaded but unused (reserved).
