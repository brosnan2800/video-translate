# Spec 01 — Data Schemas

All on-disk formats are JSON (UTF-8, `ensure_ascii=False`). Timestamps are
seconds (float), rounded to 2 decimals on write.

## Segment (in-memory: `models.Segment`)
```python
@dataclass
class Segment:
    start: float   # seconds
    end: float     # seconds
    text: str      # stripped
```
- `from_dict`: requires `start` and `end` (KeyError otherwise); `text` optional
  → coerced to stripped string, missing/None → "".
- `to_dict`: `{start, end, text}` with start/end rounded to 2 decimals.

## chunk_N.json  &  {base}.segments_en.json
Both are **lists** of segment dicts:
```json
[{"start": 2.07, "end": 5.27, "text": "..."}, ...]
```
- `chunk_N.json`: one chunk's segments, timestamps ALREADY offset by chunk start.
- `{base}.segments_en.json`: all chunks merged in order (concatenation).
- Ordering: strictly non-decreasing `start` across the merged list.

## {base}.zh_segments.json
A **dict** mapping stringified segment index (0-based) → Chinese text:
```json
{"0": "第一句中文", "1": "第二句中文", ...}
```
- Key `str(i)` corresponds to `segments_en.json[i]` (positional).
- Completeness contract: every English index `0..n-1` SHOULD have a key. Missing
  keys mean that segment failed translation (see agent_pending).

## {base}.agent_pending.json
A **list** of segments Google failed to translate:
```json
[{"index": 42, "start": 120.5, "end": 123.0, "text": "..."}]
```
- Empty list `[]` means full success (no agent backfill needed).

## Output files (stage 3) — see 04-generate-srt.md for exact bytes.
- `{base}.bilingual.srt`, `{base}.zh.srt`, `{base}.en.srt`, `{base}.txt`
