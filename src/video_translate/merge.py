"""Segment merge + V3 word-level split.

INVARIANT: timestamps are acoustic facts, never recomputed. Merge takes the
group's first word-start / last word-end (real boundaries); split cuts only at
word boundaries (never inside a word). See Spec 13 / ADR-009.

References (parameter set only, no dependency added):
- stable-ts (`jianfch/stable-ts`): merge_by_gap / split_by_punctuation /
  split_by_length pattern.
- WhisperX (`m-bain/whisperX`): single line <= 42 chars (the 剪映 limit).
V3: word-level timestamps come from faster-whisper (word_timestamps=True);
split_by_length / split_by_gap are implemented here as pure functions so we
keep zero extra dependencies (ADR-008 fallback route A).
"""
from __future__ import annotations

import re
from typing import Any

from .io_utils import load_json, save_json

DEFAULT_MAX_DUR = 8.0      # seconds, single cue upper bound
DEFAULT_MAX_GAP = 0.5      # seconds, gap below which neighbors merge
DEFAULT_MAX_CHARS = 42     # chars, 剪映单行上限; V3 splits cues longer than this
DEFAULT_SPLIT_GAP = 1.0    # seconds, intra-segment silence that triggers a split
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

    max_chars is NOT a merge gate — it is a split constraint (Spec 13). Splitting
    happens after merge in split_long_cues.

    Returns a new list; input is not mutated. Each merged seg carries the
    concatenated `words` and is tightened to the first/last word boundary.
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
    """Emit a merged segment, carrying and tightening word boundaries.

    If the group has word timestamps, the cue is tightened to the first word's
    start / last word's end — this drops the leading silence that VAD pads onto
    a segment (fixes the "cue appears early" symptom, Spec 15 / ADR-009). Without
    words we fall back to segment-level boundaries.
    """
    texts = [(s.get("text") or "").strip() for s in group]
    words: list[dict[str, Any]] = []
    for s in group:
        words.extend(s.get("words") or [])
    if words:
        start = words[0]["start"]
        end = words[-1]["end"]
    else:
        start = group[0]["start"]
        end = group[-1]["end"]
    seg: dict[str, Any] = {
        "start": start,
        "end": end,
        "text": " ".join(texts),
    }
    if words:
        seg["words"] = words
    return seg


# --------------------------- V3: word-level split ---------------------------

def _joined(words: list[dict[str, Any]]) -> str:
    return " ".join((w.get("word") or "").strip() for w in words)


def _subcue(word_group: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "start": word_group[0]["start"],
        "end": word_group[-1]["end"],
        "text": _joined(word_group),
        "words": list(word_group),
    }


def _split_by_gap(words: list[dict[str, Any]], max_gap: float = DEFAULT_SPLIT_GAP):
    """Break a word list wherever the inter-word silence exceeds `max_gap`.

    This is how real pauses survive into the subtitle timeline (issue #001): a
    long segment that swallows a scene-break silence is split there, so the SRT
    shows two cues with the silence between them.
    """
    if not words:
        return []
    groups: list[list[dict[str, Any]]] = [[words[0]]]
    for w in words[1:]:
        if w["start"] - groups[-1][-1]["end"] > max_gap:
            groups.append([w])
        else:
            groups[-1].append(w)
    return groups


def _split_by_length(words: list[dict[str, Any]], max_chars: int = DEFAULT_MAX_CHARS):
    """Break a word list into groups whose joined length <= max_chars, cutting
    only at word boundaries (never inside a word)."""
    groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    cur_len = 0
    for w in words:
        wl = len((w.get("word") or "").strip())
        if cur and cur_len + wl > max_chars:
            groups.append(cur)
            cur = []
            cur_len = 0
        cur.append(w)
        cur_len += wl
    if cur:
        groups.append(cur)
    return groups


def split_long_cues(
    segs: list[dict[str, Any]],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_gap: float = DEFAULT_SPLIT_GAP,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """V3: after merge, split over-long / silence-spanning cues at word level.

    Order (Spec 13 invariant: merge first, then split — irreversible):
      1. _split_by_gap  — break at real intra-segment silences (issue #001).
      2. _split_by_length — break over-long groups to <= max_chars (剪映 limit).
    Sub-cue timestamps are word boundaries, never recomputed.
    """
    if not enabled:
        return segs
    out: list[dict[str, Any]] = []
    for s in segs:
        words = s.get("words")
        if not words or len(words) < 2:
            out.append(s)
            continue
        # 1) gap split ALWAYS runs — real scene-break silence must survive into
        #    the timeline even for short cues (issue #001). It is NOT length-gated:
        #    _split_by_gap only breaks on inter-word silence > max_gap, which normal
        #    speech never produces, so ordinary cues are untouched.
        groups = _split_by_gap(words, max_gap)
        # 2) length split within each gap-group (剪映 single-line <= max_chars).
        final: list[list[dict[str, Any]]] = []
        for g in groups:
            if len(_joined(g)) <= max_chars:
                final.append(g)
            else:
                final.extend(_split_by_length(g, max_chars))
        if len(final) <= 1:
            out.append(s)
            continue
        out.extend(_subcue(g) for g in final)
    return out


def apply_merge(
    segments_path: str,
    *,
    raw_path: str,
    max_dur: float = DEFAULT_MAX_DUR,
    max_gap: float = DEFAULT_MAX_GAP,
    respect_sentence_end: bool = True,
    split_enabled: bool = True,
    split_max_chars: int = DEFAULT_MAX_CHARS,
    split_max_gap: float = DEFAULT_SPLIT_GAP,
) -> str:
    """Pipeline hook: read `segments_path` (raw), save a copy to `raw_path`,
    merge, optionally split, and overwrite `segments_path` with the result.

    Returns segments_path. Used by the CLI after transcribe; `--no-merge` skips
    this entirely; `--no-split` keeps merge but disables cue splitting.
    """
    raw = load_json(segments_path)
    save_json(raw_path, raw, indent=0)
    merged = merge_segments(
        raw, max_dur=max_dur, max_gap=max_gap,
        respect_sentence_end=respect_sentence_end,
    )
    if split_enabled:
        merged = split_long_cues(
            merged, max_chars=split_max_chars, max_gap=split_max_gap, enabled=True,
        )
    save_json(segments_path, merged, indent=0)
    return segments_path
