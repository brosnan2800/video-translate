# ADR-004 — Segment merge strategy (default ON, timestamp-preserving)

Date: 2026-07-19 · Status: accepted

## Context
V1 transcribe emits Whisper's native segments. For some content Whisper
over-fragments a sentence across cues with no terminal punctuation, producing
jumpy subtitles. The user reported "碎句" problems (with an earlier tool) and
requested a merge layer in V2.

## Decision
Add a pure `merge_segments` post-processor, default ON, that merges adjacent
fragments when: gap < 0.5s, combined duration ≤ 8s, and the group does not end
with sentence punctuation. Merged timestamps are taken verbatim from the input
(first start / last end) — never recomputed.

## Why timestamp-preserving (not recomputed)
Timestamps are acoustic facts (Spec 00 invariant). Recomputing them would risk
audio/subtitle desync — the exact bug the user complained about in the earlier
tool. Taking the group's real first-start / last-end preserves alignment for
free. Cross-chunk stitching is naturally handled because `merge_chunks` flattens
chunks before merge runs.

## Why max_dur=8.0 / max_gap=0.5
- 8.0s: a single subtitle cue readable in one glance (industry norm).
- 0.5s: Whisper VAD's default min silence; a gap ≥ 0.5s likely indicates a real
  pause / sentence boundary. stable-ts and WhisperX use comparable thresholds.

## Why max_chars is NOT a merge gate
`max_chars=42` (剪映 single-line limit) is a *split* constraint (stable-ts
`split_by_length`). Splitting needs word-level timestamps to redivide a too-long
cue's time — V2 has none. Blocking merges on max_chars made merge a no-op on
apollo (209→209, because 161/209 segments already end a sentence and 82/209
exceed 42 chars). max_dur already bounds length; splitting is deferred to v3
(needs forced alignment, à la WhisperX).

## Consequences
- merge is a no-op on already-well-segmented input (correct — nothing to fix).
- On fragmented input it rejoins mid-sentence pieces without touching timestamps.
- `merge_max_chars` config field is loaded but reserved (v3).
