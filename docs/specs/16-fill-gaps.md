# Spec 16 — Coverage self-audit & gap recovery (`fill_gaps`)

Module: `fill_gaps.py`. Runs automatically after `merge` inside `transcribe` /
`run` (default ON; `--no-audit` to skip). Decision V11. Counterpart of
[Spec 17](17-verify-align.md) (which guards the *translation* side); this spec
guards the *transcription* side.

## Problem
Even with `no_speech_threshold=0` (see `transcribe.py`), faster-whisper can
still drop audible speech — most often stylized / sung / impression audio it
scores as "non-speech", or a chunk-edge artifact. Two independent loss modes:

1. **Time holes** — stretches of the timeline with no segment at all:
   - `HEAD`: silence before the first segment (`0.0 → seg[0].start`)
   - `interior`: the gap between two adjacent segments
   - `TAIL`: silence after the last segment (`seg[-1].end → video duration`)
2. **In-segment collapse** — *no* time hole (timeline stays continuous) but a
   long segment whose text holds only a fragment; the rest of its speech was
   silently swallowed. Detected by character density (chars/sec) against the
   file's own median, not an absolute threshold.

A third, subtler loss mode — **prefix collapse** — is handled inside the probe
(see below), not as a separate audit pass.

## Algorithm (`fill_gaps`)
1. **Collect holes** — HEAD / interior / TAIL windows where the gap exceeds
   `min_gap` (default **2.0 s**). Also collect in-segment collapses via
   `find_collapsed()`.
2. **Force-decode each suspect window** with `no_speech_threshold=0.0` (silence
   is never auto-suppressed) and `temperature` fallback. Each decode is run with
   `HF_HUB_OFFLINE=1` (local model cache only) and reuses the production recipe
   (`BEAM_SIZE`, `BEST_OF`, `CONDITION_ON_PREVIOUS_TEXT`, `REPETITION_PENALTY`).
   Recovered segments carry an `_recovered=True` marker and absolute timestamps
   shifted by the window start.
3. **Echo dedup** — `_is_echo()` drops text that merely leaks a neighbour line
   (whisper's decoder bleeding the adjacent cue into the quiet gap). Heuristics,
   cheapest first: `<2` words; exact containment either direction; token Jaccard
   `>0.6`; character-level `difflib.SequenceMatcher > 0.7`. The last test matters
   because whisper transcribes the *same* utterance differently across a boundary
   (e.g. `"he bit my earl."` vs `"He bit my ear off."` scores only 0.5 on Jaccard
   yet 0.78 on characters).
4. **Collapsed replacement** — a collapsed segment is replaced by its re-decode
   only when the decode yields materially more speech (`len(recovered) >= 2` **or**
   `new_chars > 1.6 × orig_chars`); otherwise the original is kept.
5. **Merge & sort** — recovered inserts + kept originals, sorted by `start`.

### Recovery is ALWAYS bare (ADR-016 / T2a)

The forced decode in `_decode_once` **hard-codes `vad_filter=False`** and keeps
`no_speech_threshold=0.0`. The run's `--vad` flag is **never** propagated into
the recovery decode. Rationale (Spec 16 §Invariant originally stated this but the
wiring contradicted it): forcing VAD here re-introduces the exact drop this module
exists to fix — speech sitting under laughter / cheer / music is ejected by VAD.
On `emily-blunt.mp4` the five cheer-masked windows (4:06, 4:24, 4:59, 6:15,
15:29) were recovered only because the recovery ran bare. See also
[ADR-015](adr:/015-adaptive-per-chunk-vad.md) for preventing the miss upstream.

### Prefix-collapse probe (`_probe`)
Whisper latches onto whatever sits at the *start* of the decode window: if the
pad reaches back far enough to catch the previous line's tail, the decoder emits
that fragment then predicts end-of-transcript for the whole remaining window. So
the probe does **not** bet on one pad. It tries `_PROBE_PADS = (0.2, 0.0, 0.5)`
(windows `< _MULTI_PROBE_MIN_WINDOW = 4.0 s` get a single decode with pad `0.2`),
scores each result by how much of the window it covers, and keeps the best. Scan
stops early once a probe covers `≥ _PROBE_GOOD_COVERAGE = 0.6` of the window, so
the common case still costs a single decode.

### Long-hole sub-windowing (`_probe_long_hole`, ADR-016 T2c)
A single forced decode over a **very wide** hole (e.g. a 40 s gap) is itself
unreliable: whisper collapses it into one fragment or hallucinates, and the pad
rotation above cannot rescue it. So any hole wider than `_SUBWIN = 12.0 s` is
**sliced** into sub-windows of at most `_SUBWIN` seconds, with `_SUBWIN_OVERLAP
= 0.5 s` overlap so a sentence straddling a cut is not clipped. Each sub-window
is force-decoded independently with a small pad (`_PROBE_PADS[0] = 0.2`) — a
neighbour can only leak a fraction of a second into a sub-window, so echo is
naturally bounded there. Adjacent sub-windows decode the overlap region twice;
`_dedupe_seams()` drops the duplicate (time-overlap > 0.2 s **and** text
similarity > 0.5, via `_text_sim` = Jaccard falling back to char-ratio) and trims
any residual overlap to the earlier window. This is the B-direction recall
hardening that recovers speech hidden inside the large uncovered-audio windows
that `verify`'s acoustic lane flags (seen on `Everybody.Loves.Raymond.S01E04`,
a 41 s gap at 922→963 s).

## Invariant (load-bearing)
- **Timestamps stay acoustic facts.** Recovered segments reuse the whisper
  word-level timestamps from the forced decode, only shifted by the absolute
  window start; no downstream recomputation.
- **Audit is idempotent-ish.** If no hole (`≥ min_gap`) and no collapse is found,
  the input is returned unchanged and the audit is essentially free.
- **Content type, not length, decides VAD.** The probe always runs with
  `vad_filter = use_vad` (default `False`), i.e. the same VAD-off default as the
  main transcription (see ADR-011). Forcing VAD here would re-introduce the very
  drop this module exists to fix.

## Defaults
| Param | Default | Notes |
|---|---|---|
| `min_gap` | `2.0` | minimum hole width to probe |
| `collapse_min_dur` | `4.0` | a segment must be this long to be a collapse candidate |
| `collapse_ratio` | `0.45` | cps below `median × ratio` ⇒ collapse |
| `_PROBE_PADS` | `(0.2, 0.0, 0.5)` | prefix-collapse mitigation |
| `_SUBWIN` | `12.0` | holes wider than this are sliced into sub-windows (ADR-016 T2c) |
| `_SUBWIN_OVERLAP` | `0.5` | overlap between sub-windows so cuts don't clip sentences |
| `use_vad` | `False` | **ignored by the decode** — recovery is always bare (ADR-016) |

CLI: `--no-audit` (on `transcribe` / `run`) skips the whole pass.

## Golden
No dedicated golden fixture yet — the pass is non-deterministic (model decode)
and exercised manually on the Jamie Foxx fan-edit. Its outputs are validated by
the downstream [Spec 17](17-verify-align.md) alignment guard and by human review
of the recovered cues.
