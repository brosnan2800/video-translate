"""Unified self-check gate — three orthogonal lanes (Spec 18 / ADR-012).

Subtitle correctness has three independent layers; this module checks each
against an *independent* reference instead of trusting whisper's self-asserted
timestamps:

  - acoustic (声学层): each cue vs measured silence (`silencedetect`). Flags
    cues that sit inside silence, cross a silence boundary, or fire before the
    first real utterance.
  - content (内容层): coverage (`validate_zh`) + index drift (`verify_align`).
    Reused from existing modules; see `cmd_verify` in cli.py.
  - presentation (表现层): display-window sanity — `tail`/`min_dur` stripped to
    0 (over-tightened window → early-vanish / flash), and first-cue-early.

All acoustic/presentation helpers here are pure (no subprocess) and unit-tested
with synthetic silence intervals + cue sequences.
"""
from __future__ import annotations

from typing import Any

# Issue types
IN_SILENCE = "in-silence"
CROSS_SILENCE = "cross-silence"
FIRST_CUE_EARLY = "first-cue-early"
TAIL_STRIPPED = "tail-stripped"
MIN_DUR_STRIPPED = "min-dur-stripped"

_EPS = 1e-3


def cue_bounds(segments: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Resolve each cue's acoustic window (word-level if present, else segment)."""
    bounds: list[tuple[float, float]] = []
    for s in segments:
        words = s.get("words")
        if words:
            st, en = words[0]["start"], words[-1]["end"]
        else:
            st, en = s.get("start", 0.0), s.get("end", 0.0)
        bounds.append((st, en))
    return bounds


def cue_in_silence(start: float, end: float,
                   silences: list[tuple[float, float]]) -> bool:
    """True when the whole cue window lies inside one silence interval."""
    for (s0, s1) in silences:
        if start >= s0 - _EPS and end <= s1 + _EPS:
            return True
    return False


def cue_cross_silence(start: float, end: float,
                      silences: list[tuple[float, float]],
                      min_cross_dur: float = 1.0) -> bool:
    """True when a *substantial* silence interval lies strictly inside the cue.

    This is the drift symptom: whisper bridged a real pause, so the cue straddles
    a silence that should have separated two utterances. A silence interval is
    only counted when it is fully contained within the cue AND lasts at least
    `min_cross_dur` seconds — so the ordinary short gaps between words/sentences
    (which every cue overlaps at its edges) are NOT flagged.

    Args:
        min_cross_dur: minimum silence length (s) that counts as a real pause.
    """
    for (s0, s1) in silences:
        if (s1 - s0) < min_cross_dur:
            continue
        if s0 >= start + _EPS and s1 <= end - _EPS:
            return True
    return False


def first_cue_early(first_start: float, silences: list[tuple[float, float]],
                   offset: float = 0.0) -> bool:
    """True when the first cue fires before real speech begins.

    Real speech begins after the leading silence interval (the one starting at
    ~0). The cue's *display* start is `first_start - offset`.
    """
    disp = first_start - offset
    if disp < 0:
        disp = 0.0
    for (s0, s1) in sorted(silences):
        if s0 <= _EPS:  # leading silence at the very start
            return disp < s1 - _EPS
    return False


def verify_acoustic(segments: list[dict[str, Any]],
                    silences: list[tuple[float, float]],
                    offset: float = 0.0,
                    min_cross_dur: float = 1.0) -> list[dict[str, Any]]:
    """Acoustic lane. Returns one issue dict per flagged cue."""
    issues: list[dict[str, Any]] = []
    if not silences:
        return issues
    bounds = cue_bounds(segments)
    for i, (st, en) in enumerate(bounds):
        if cue_in_silence(st, en, silences):
            issues.append({"index": i, "type": IN_SILENCE,
                           "start": st, "end": en})
        elif cue_cross_silence(st, en, silences, min_cross_dur=min_cross_dur):
            issues.append({"index": i, "type": CROSS_SILENCE,
                           "start": st, "end": en})
    if bounds and first_cue_early(bounds[0][0], silences, offset):
        issues.append({"index": 0, "type": FIRST_CUE_EARLY,
                       "start": bounds[0][0], "end": bounds[0][1]})
    return issues


def verify_presentation(opts: dict[str, float],
                        first_start: float | None,
                        silences: list[tuple[float, float]],
                        offset: float = 0.0) -> list[dict[str, Any]]:
    """Presentation lane. Flags over-tightened display window + first-cue-early."""
    issues: list[dict[str, Any]] = []
    tail = float(opts.get("tail", 0.0) or 0.0)
    min_dur = float(opts.get("min_dur", 0.0) or 0.0)
    if tail == 0.0:
        issues.append({"type": TAIL_STRIPPED,
                       "detail": "tail=0 (default 0.3) — cue may vanish before speech ends"})
    if min_dur == 0.0:
        issues.append({"type": MIN_DUR_STRIPPED,
                       "detail": "min_dur=0 (default 1.0) — very short cues flash"})
    if first_start is not None and silences and first_cue_early(first_start, silences, offset):
        issues.append({"type": FIRST_CUE_EARLY, "start": first_start})
    return issues


def build_semantic_reread_task(
    segments: list[dict[str, Any]],
    zh: dict[int, str],
    *,
    window: int = 2,
) -> dict[str, Any]:
    """Build an agent-side semantic reread task (ADR-005: CLI never calls an LLM).

    The task lists every segment's English source + its Chinese translation plus
    a small neighbour context window, so the *calling agent* (which has LLM
    access) can reread each pair and flag omissions / additions / mistranslations
    — the third content-layer check (`validate_zh` covers coverage,
    `verify_align` covers index drift; this covers fidelity). Returns a dict ready
    to be serialised to ``<base>.semantic_reread_task.json``.
    """
    pairs: list[dict[str, Any]] = []
    n = len(segments)
    for i, s in enumerate(segments):
        idx = i  # zh is 0-based keyed by segment position
        en = (s.get("text") or "").strip()
        cn = (zh.get(idx) or "").strip()
        if not en and not cn:
            continue
        context_before = [ (segments[j].get("text") or "").strip()
                           for j in range(max(0, i - window), i) ]
        context_after = [ (segments[j].get("text") or "").strip()
                          for j in range(i + 1, min(n, i + 1 + window)) ]
        pairs.append({
            "index": idx,
            "en": en,
            "zh": cn,
            "context_before": context_before,
            "context_after": context_after,
        })
    return {
        "task": "semantic-reread",
        "persona": "You are a rigorous translator. Re-read each (en, zh) pair and "
                   "flag any omission, addition, or mistranslation. Output only the "
                   "indices that are wrong and why.",
        "pairs": pairs,
        "output_schema": {"<index>: <'ok' | 'omit' | 'add' | 'wrong': reason>": "..."},
    }
