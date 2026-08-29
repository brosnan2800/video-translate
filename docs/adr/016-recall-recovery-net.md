# ADR-016 — Recall recovery net (fill_gaps always-bare + verify uncovered-audio)

Date: 2026-08-19
Status: Accepted
Companion: ADR-015 (preventive per-chunk routing). This ADR is the *reactive*
safety net that complements it.

## Context

Two gaps in the recall pipeline surfaced on `emily-blunt.mp4`:

1. **fill_gaps re-applies the run's VAD.** `cli.py` passes
   `use_vad=getattr(args, "vad", False)` into `fill_gaps(...)`
   (`src/video_translate/cli.py:275-296`). `fill_gaps._decode_once` then decodes
   recovery windows with `vad_filter=use_vad`. When the run used `--vad`, the
   recovery re-decodes with VAD **on** — re-ejecting the very speech-under-noise
   it exists to recover. Spec 16 §Invariant (lines 63-66) already *states* the
   probe "always runs with `vad_filter = False`", but the wiring contradicts it.
   `resegment --no-vad` on the missed windows recovered everything, proving the
   recovery pass must be bare.

2. **verify cannot see a miss.** The acoustic lane flags cues that land *in*
   silence (IN_SILENCE / CROSS_SILENCE). A *missing* segment leaves no cue, so a
   region of real (non-silent) audio with zero coverage is never flagged. The
   user wanted "从头到尾再去捞一遍" — a detection pass over the whole timeline
   for "audio present but no subtitle".

## Decision

### 2a. fill_gaps recovery is always bare (T2a)

`fill_gaps._decode_once` hard-codes `vad_filter=False` (and keeps
`no_speech_threshold=0.0`). The `use_vad` argument is dropped from the decode
path; `fill_gaps()` may still accept `use_vad` for CLI compatibility but it no
longer influences the forced decode. This makes Spec 16's invariant true in code.

### 2b. verify gains an uncovered-audio lane (T2b)

New pure function `find_uncovered_speech(segments, silence_intervals, duration,
min_dur=2.0)`:
1. Union-merge cue coverage `[start,end]` across all segments.
2. Compute the complement → timeline stretches with **no cue**.
3. From those, keep only the sub-stretches that are **not silence**
   (audio is present) and ≥ `min_dur`.

These become `UNCOVERED_AUDIO` issues, reported as part of the acoustic lane in
`verify`. This is *detection only* (Spec 18 invariant: verify never rewrites),
so it alarms a missed region for the agent to re-run recovery — it does not
re-decode.

### 2c. fill_gaps slices very wide holes into sub-windows (T2c, B direction)
`verify`'s uncovered-audio lane (T2b) still flagged a **large** missed region on
`Everybody.Loves.Raymond.S01E04` — a 41 s gap at 922→963 s. That hole was *not*
genuine silence (so it was force-decoded), but a single forced decode over 41 s
collapsed / hallucinated and recovered nothing. So `fill_gaps` now routes any
hole wider than `_SUBWIN = 12.0 s` to `_probe_long_hole()`, which slices it into
`≤12 s` sub-windows (with `_SUBWIN_OVERLAP = 0.5 s`) and force-decodes each
independently with a small pad; `_dedupe_seams()` drops the duplicate overlap
fragment and trims residual overlap. This is the recall-hardening B direction
the agent chose over manual backfill (A) and ship-as-is (C). T2b stays
detect-only; T2c strengthens the *same* recovery pass it feeds.

## Consequences

- `resegment --no-vad` + `fill_gaps` (now bare) recovers masked speech at the
  recovery stage even when the upstream run used `--vad`.
- Holes wider than 12 s are now sliced and decoded per sub-window, recovering
  speech that a single wide forced-decode previously collapsed (B direction;
  closes the 41 s Raymond gap).
- `verify` now flags "audio present but unsubtitled" regions, closing the
  false-clean hole where a miss passed all three lanes.
- No golden regression: `find_uncovered_speech` is a pure function with unit
  tests; fill_gaps decode change is covered by a mock test asserting
  `vad_filter=False`.

## Rejected alternatives

- *Put the re-decode inside `verify`*: violates Spec 18 "只检测不重写" and runs
  after translation, forcing a re-translate/re-generate loop. Recovery belongs in
  `fill_gaps` (pre-translation), per ADR-015/016 division of labour.
- *Lower `silencedetect` noise floor globally*: would surface more holes but also
  more false holes; per-chunk routing (ADR-015) is the principled fix.
