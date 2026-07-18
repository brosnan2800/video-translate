# Spec 04 — Generate Subtitles

Module: `generate.py` (+ `srt_utils.py`, `io_utils.py`). Produces the four
Jianying(剪映)-importable deliverables. **This is the golden-regression stage:
output must be byte-for-byte identical to `docs/golden/apollo_story.*`.**

## Inputs
- `{base}.segments_en.json` — list of `{start, end, text}` (Spec 01).
- `{base}.zh_segments.json` — `{str_index: zh}` mapping (Spec 03).

## Outputs (written into `outdir`, prefixed with `base`)
| Suffix          | Content                                           |
|-----------------|---------------------------------------------------|
| `.bilingual.srt`| Chinese line on top, English line below           |
| `.zh.srt`       | Chinese-only cues                                 |
| `.en.srt`       | English-only cues                                 |
| `.txt`          | Flat review file (`[t0 -> t1]` + 中文/英文 lines) |

## Algorithm (`build_outputs(segments, zh) -> {suffix: content}`)
1. Enumerate segments 1-based (`i` from 1).
2. `en_t = (segment.text or "").strip()`; `cn = (zh.get(i-1) or "").strip()`.
   - **Index mapping is positional**: `zh[i-1]` corresponds to segment `i`.
3. Bilingual cue: `block(i, start, end, [cn, en_t])` — empty lines dropped, so a
   segment with no Chinese still appears with just the English line.
4. `.zh.srt` cue is emitted **only if** `cn` is non-empty; `.en.srt` cue only if
   `en_t` is non-empty.
5. `.txt` always appends `[t0 -> t1]\n中文: {cn}\n英文: {en_t}\n` for every segment.
6. Each file = cues joined by `\n`, `.rstrip()`, then a single trailing `\n`.

## Timestamp formatting (`srt_utils.srt_time`)
- Verbatim from the original `gen_srt.py`. `HH:MM:SS,mmm`.
- Millisecond carry fix: if rounding yields `ms == 1000`, roll into the next
  second (`s += 1; ms = 0`). This edge case is covered by a unit test.

## Design invariant
Timestamps come straight from the acoustic transcription; this stage NEVER
recomputes them. It only lays text into cues. That is what keeps subtitles glued
to the audio even after translation rewrites the words.

## Contract (golden, no network, no model)
- Feeding `docs/golden/apollo_story.segments_en.json` +
  `apollo_story.zh_segments.json` reproduces all four golden files **byte-exact**
  (verified: 20409 / 10846 / 16769 / 21562 bytes).
- `write_text` is atomic (temp file + `os.replace`), `ensure_ascii=False`.
