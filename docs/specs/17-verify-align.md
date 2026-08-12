# Spec 17 — zh/en index-drift guard (`verify_align`)

Module: `verify_align.py`. Runs automatically inside `generate` (default ON;
`--no-align-check` to silence). Decision V12. Counterpart of
[Spec 16](16-fill-gaps.md) (transcription coverage); this spec guards the
*translation* index alignment.

## Problem
Agent translation is produced batch-by-batch, keyed by segment index. If a
single line is skipped or duplicated while writing a batch, **every subsequent
translation shifts by one**. The English track stays correct (it is untouched),
so the failure is invisible in pipeline logs — it only surfaces when a human
watches the Chinese track and notices lines drifting out of sync. "Three checks
all pass" (segment count, contiguous indices, no nulls, valid SRT) does **not**
catch this; the pipeline fails silently.

## Algorithm
Two language-agnostic signals, no per-video glossary required:

1. **Length-profile correlation (`check_alignment`)** — load-bearing signal.
   Per-segment source length and translation length correlate strongly when
   aligned. Over a sliding window (`window = 24`, step `window // 2`), compare
   the Pearson correlation at shift `0` against shifts `±1, ±2`
   (`max_shift = 2`). If a shifted pairing explains the data better by more than
   `margin = 0.20`, the region is flagged with `shift = s`, meaning
   `zh[i+s]` pairs with `en[i]` better than `zh[i]` does. `shift = -1` is the
   classic "translator skipped a line" case: every `zh[i]` actually belongs to
   `en[i+1]`. Adjacent windows reporting the same shift are merged.
2. **Digit co-occurrence (`check_digits`)** — a digit present in the source
   should also appear in its translation. If it goes missing here but turns up at
   offset `±1 / ±2`, that is a direct fingerprint of an off-by-N shift. Flags
   `found_at` for the neighbour containing the digits.

`report()` runs both checks and prints a warning summary. It returns `True` when
clean, `False` when any drift/digit flag fires — but it is **warning-only**: it
never blocks the render, letting the caller decide.

## Invariant (load-bearing)
- **English track is the ground truth.** The guard only ever *detects*; it never
  rewrites `zh` or `en`. Misalignment is reported for human/agent correction.
- **No vocabulary needed.** Signal 1 (length correlation) works for any language
  pair, including zh/en, because it uses character counts, not words.
- **Non-fatal.** A flagged region is a warning printed to stderr; `generate`
  proceeds. This keeps the pipeline usable on borderline cases while surfacing
  the risk.

## Wiring
`cli.cmd_generate` loads `args.segments` (`segments_en.json`) and `args.zh`
(`zh_segments.json`) as JSON and calls `report()`. Skipped entirely when
`--no-align-check` is passed. `make test` covers the pure functions
(`check_alignment`, `check_digits`) with synthetic shifted fixtures.

## Defaults
| Param | Default | Notes |
|---|---|---|
| `window` | `24` | sliding-window size for correlation |
| `margin` | `0.20` | min correlation gain to flag a shift |
| `max_shift` | `2` | shifts tested: `-2..+2` |

CLI: `--no-align-check` (on `generate`) silences the guard.

## Golden
Unit-tested with synthetic fixtures (clean, off-by-1, off-by-2, digit-shifted)
in `tests/`; no byte-exact golden fixture (pure analysis, no file output).
