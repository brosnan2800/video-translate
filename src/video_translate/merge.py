"""Segment merge: glue over-fragmented Whisper cues into readable subtitle units.

INVARIANT: merged.start = first segment's start; merged.end = last segment's end.
Timestamps are never recomputed — only the text is concatenated (single space).

References (parameter set only, no dependency added):
- stable-ts (`jianfch/stable-ts`): merge_by_gap / split_by_punctuation pattern.
- WhisperX (`m-bain/whisperX`): single line <= 42 chars (the 剪映 limit).
This is a lightweight JSON post-processor; faster-whisper is untouched.
"""
from __future__ import annotations

import re
from typing import Any

from .io_utils import load_json, save_json

DEFAULT_MAX_DUR = 8.0      # seconds, single cue upper bound
DEFAULT_MAX_GAP = 0.5      # seconds, gap below which neighbors are candidates
DEFAULT_MAX_CHARS = 42     # chars, 剪映单行上限 — RESERVED for future v3 split
                           # (needs word-level timestamps; V2 cannot split)
_SENT_END = re.compile(r"[.!?]\s*$")


def merge_segments(
    segs: list[dict[str, Any]],
    *,
    max_dur: float = DEFAULT_MAX_DUR,
    max_gap: float = DEFAULT_MAX_GAP,
    respect_sentence_end: bool = True,
) -> list[dict[str, Any]]:
    """Merge adjacent fragmented segments.

    Rules (ALL must hold to merge seg[i] into the current group):
      1. gap = seg[i].start - group.end < max_gap
      2. (seg[i].end - group.start) <= max_dur
      3. if respect_sentence_end: group's last text does NOT end with [.!?]
      4. seg[i].start >= group.end (no overlap; chunks are monotonic)

    Note: max_chars is intentionally NOT a merge gate. It is a split constraint
    (stable-ts split_by_length); V2 has no word-level timestamps to split a too-
    long cue, so blocking merges on it would prevent fragments from rejoining
    into sentences. max_dur already bounds merged length indirectly. Splitting is
    deferred to v3.

    Returns a new list; input is not mutated. Each merged seg =
    {start: group[0].start, end: group[-1].end, text: " ".join(texts)}.
    """
    if not segs:
        return []
    out: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = [segs[0]]
    for s in segs[1:]:
        gap = s["start"] - cur[-1]["end"]
        would_dur = s["end"] - cur[0]["start"]
        ends_sent = _SENT_END.search((cur[-1].get("text") or "").strip()) is not None
        if (gap < max_gap
                and would_dur <= max_dur
                and not (respect_sentence_end and ends_sent)
                and s["start"] >= cur[-1]["end"]):
            cur.append(s)
        else:
            out.append(_emit(cur))
            cur = [s]
    out.append(_emit(cur))
    return out


def _emit(group: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "start": group[0]["start"],
        "end": group[-1]["end"],
        "text": " ".join((s.get("text") or "").strip() for s in group),
    }


def apply_merge(
    segments_path: str,
    *,
    raw_path: str,
    max_dur: float = DEFAULT_MAX_DUR,
    max_gap: float = DEFAULT_MAX_GAP,
    respect_sentence_end: bool = True,
) -> str:
    """Pipeline hook: read `segments_path` (raw), save a copy to `raw_path`,
    merge, and overwrite `segments_path` with the merged result.

    Returns segments_path. Used by the CLI after transcribe; `--no-merge` skips
    this entirely (transcribe output stays as-is, no raw copy written).
    """
    raw = load_json(segments_path)
    save_json(raw_path, raw, indent=0)
    merged = merge_segments(
        raw, max_dur=max_dur, max_gap=max_gap,
        respect_sentence_end=respect_sentence_end,
    )
    save_json(segments_path, merged, indent=0)
    return segments_path
