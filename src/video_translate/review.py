"""ADR-034 §6.2 — post-transcribe dual-signal review (pure decision layer).

Two *independent* signals are combined, and the acoustic reference (**B**) is the
main judge:

* **A — Whisper self-report (2-D)**: ``no_speech_prob`` (high ``>= 0.6`` means the
  model heard energy but not clear speech) and ``avg_logprob`` (low ``< -1.0``
  means a low-confidence / hallucination-prone decode). A only answers
  "is this abnormal"; it does NOT say what to do about it.
* **B — independent reference**: the ORIGINAL video's ``silencedetect`` intervals
  (never the cleaned/demucs source — Spec 19 Invariant #4). A window that is not
  covered by detected silence carries acoustic energy; a window fully inside
  silence should carry no speech.

Verdict table (ADR-034 §3.1)::

    B = energetic  + A abnormal   -> MISSING       (漏译嫌疑 -> re-process G1/G2)
    B = silent     + text present -> HALLUCINATION (幻觉嫌疑 -> mark/drop)
    B = energetic  + A clean      -> CLEAN         (keep)

Why B dominates: A alone cannot distinguish "real speech the model scored badly"
from "a hallucination over noise". Only the acoustic reference knows whether
*something was actually sounding* in that window.

Practical note (merge): main-pass segments frequently lose their confidence
fields during ``merge``, so A is often ``unknown`` (neither signal fires). The
G2 path below is therefore designed to be **B-driven only** (energy + word
coverage) so it still works on merged timelines, while the G1 path keys off the
A-and-B ``MISSING`` verdict.

Everything in this module is pure (no I/O, no model, no ffmpeg) so the whole
decision surface is unit-testable with synthetic signals — see
``tests/test_review.py``.
"""
from __future__ import annotations

from typing import Any

from .artifacts import raw_sources

_EPS = 1e-3

# Verdicts.
MISSING = "missing"              # 漏译嫌疑 -> re-process (G1/G2/G3)
HALLUCINATION = "hallucination"  # 幻觉嫌疑 -> mark / drop
CLEAN = "clean"                  # keep as-is


# --------------------------------------------------------------------------- #
# interval primitives (pure)
# --------------------------------------------------------------------------- #

def _merge_intervals(
    intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Sort + merge overlapping/adjacent intervals. Pure."""
    out: list[tuple[float, float]] = []
    for (a, b) in sorted((float(x), float(y)) for (x, y) in intervals):
        if b <= a:
            continue
        if out and a <= out[-1][1] + _EPS:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract(
    pieces: list[tuple[float, float]],
    cuts: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Remove every interval in `cuts` from every piece in `pieces`. Pure.

    This is the workhorse behind both G1 (split a window at silence edges) and
    G2 (remove word coverage from a segment's span).
    """
    out: list[tuple[float, float]] = []
    for (p0, p1) in pieces:
        cur: list[tuple[float, float]] = [(p0, p1)]
        for (c0, c1) in cuts:
            if c1 <= c0:
                continue
            nxt: list[tuple[float, float]] = []
            for (a, b) in cur:
                ov = min(b, c1) - max(a, c0)
                if ov <= 0:  # disjoint: keep whole
                    nxt.append((a, b))
                    continue
                if c0 > a:  # left remainder
                    nxt.append((a, min(b, c0)))
                if c1 < b:  # right remainder
                    nxt.append((max(a, c1), b))
            cur = nxt
        out.extend(cur)
    return [(a, b) for (a, b) in out if (b - a) > _EPS]


# --------------------------------------------------------------------------- #
# signal B — acoustic energy from the independent silencedetect reference
# --------------------------------------------------------------------------- #

def _silence_cover(
    start: float, end: float, silence_intervals: list[tuple[float, float]],
) -> float:
    """Seconds of [start,end) that fall inside detected silence (clamped)."""
    if end <= start:
        return 0.0
    covered = 0.0
    for (s0, s1) in (silence_intervals or []):
        ov = min(end, s1) - max(start, s0)
        if ov > 0:
            covered += ov
    # silencedetect intervals are disjoint in practice, but clamp defensively
    # (a malformed/overlapping list must not produce a negative energy fraction).
    return min(covered, end - start)


def energy_fraction(
    start: float, end: float, silence_intervals: list[tuple[float, float]],
) -> float:
    """Fraction of [start,end) that is NOT detected silence (0.0..1.0).

    1.0 = fully energetic (no silence detected inside), 0.0 = pure silence.
    """
    if end <= start:
        return 0.0
    non_silent = (end - start) - _silence_cover(start, end, silence_intervals)
    return max(0.0, min(1.0, non_silent / (end - start)))


def is_energetic(
    start: float,
    end: float,
    silence_intervals: list[tuple[float, float]],
    *,
    min_frac: float = 0.25,
    min_abs: float = 0.25,
) -> bool:
    """Signal B: does this window carry acoustic energy (i.e. not just silence)?

    Both gates must pass, because each alone is fooled:

    * ``min_abs`` (seconds of non-silence) stops a wide, mostly-silent window
      from qualifying on a sliver of energy;
    * ``min_frac`` stops a long window whose only energy is a tiny burst from
      dragging the whole span into re-processing.
    """
    dur = end - start
    if dur <= 0:
        return False
    non_silent = dur * energy_fraction(start, end, silence_intervals)
    return non_silent >= min_abs and (non_silent / dur) >= min_frac


# --------------------------------------------------------------------------- #
# signal A — Whisper's own 2-D confidence
# --------------------------------------------------------------------------- #

def signal_a_state(
    seg: dict[str, Any],
    *,
    no_speech_thr: float = 0.6,
    logprob_thr: float = -1.0,
) -> dict[str, Any]:
    """Signal A: is Whisper's own judgement of this segment abnormal?

    Thresholds mirror the ADR-021 recovery guard (``no_speech_prob >= 0.6`` is the
    strongest single hallucination signal; ``avg_logprob < -1.0`` is the fallback
    for caches that predate ``no_speech_prob``).

    Returns ``{suspect, reasons, no_speech_prob, avg_logprob}``. A segment that
    carries **neither** field (common after ``merge`` strips them) is
    ``suspect=False`` with an empty reason list — "unknown", not "clean". The
    caller must not treat unknown as evidence of health; only B can speak then.
    """
    reasons: list[str] = []
    nsp = seg.get("no_speech_prob")
    alp = seg.get("avg_logprob")
    if nsp is not None:
        try:
            if float(nsp) >= no_speech_thr:
                reasons.append("high_no_speech_prob")
        except (TypeError, ValueError):
            nsp = None
    if alp is not None:
        try:
            if float(alp) < logprob_thr:
                reasons.append("low_avg_logprob")
        except (TypeError, ValueError):
            alp = None
    return {
        "suspect": bool(reasons),
        "reasons": reasons,
        "no_speech_prob": nsp,
        "avg_logprob": alp,
    }


def signal_a_from_raw(
    seg: dict[str, Any],
    raw_segments: list[dict[str, Any]] | None,
    *,
    no_speech_thr: float = 0.6,
    logprob_thr: float = -1.0,
) -> dict[str, Any]:
    """对合并视图段按 ``_raw_indices`` 回查 raw 源段的置信度（ADR-035 Z2）。

    merge 白名单重建丢弃了视图段自身的置信度字段，信号 A 因此在合并后时间轴
    上"失明"（G1/G3 休眠的根因）。Z2 之后视图段带 ``_raw_indices``，这里逐段
    回查 raw。判定规则保守：**任一源段可疑 ⇒ 整段可疑**（宁可多查不可漏查），
    reasons 带源段下标（``raw#2:high_no_speech_prob``）便于定位到具体源段。
    """
    reasons: list[str] = []
    nsp: float | None = None
    alp: float | None = None
    for i, r in enumerate(raw_sources(seg, raw_segments)):
        sub = signal_a_state(r, no_speech_thr=no_speech_thr,
                             logprob_thr=logprob_thr)
        if sub["suspect"]:
            reasons.extend(f"raw#{i}:{x}" for x in sub["reasons"])
        if nsp is None and sub["no_speech_prob"] is not None:
            nsp = sub["no_speech_prob"]
        if alp is None and sub["avg_logprob"] is not None:
            alp = sub["avg_logprob"]
    return {
        "suspect": bool(reasons),
        "reasons": reasons,
        "no_speech_prob": nsp,
        "avg_logprob": alp,
    }


# --------------------------------------------------------------------------- #
# the review pass
# --------------------------------------------------------------------------- #

def review_segments(
    segments: list[dict[str, Any]],
    silence_intervals: list[tuple[float, float]] | None = None,
    *,
    no_speech_thr: float = 0.6,
    logprob_thr: float = -1.0,
    min_energy_frac: float = 0.25,
    min_energy_abs: float = 0.25,
    min_sub: float = 1.0,
    raw_segments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Dual-signal review over a segment list. Pure (no I/O).

    Returns one record per *non-clean* segment::

        {"index", "start", "end", "verdict", "energetic", "reasons",
         "g1_windows"}      # g1_windows only for MISSING

    ``g1_windows`` are the silence-sliced sub-windows G1 should re-decode; an
    empty list means "no silence gap inside → G1 cannot cut this window"
    (ADR-034 §3.2), so the caller should fall through to G2/G3.

    Segments missing ``start``/``end`` are skipped.
    """
    silences = list(silence_intervals or [])
    out: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        if seg.get("start") is None or seg.get("end") is None:
            continue
        try:
            s, e = float(seg["start"]), float(seg["end"])
        except (TypeError, ValueError):
            continue
        if e <= s:
            continue

        energetic = is_energetic(
            s, e, silences, min_frac=min_energy_frac, min_abs=min_energy_abs
        )
        # ADR-035 Z2: 合并视图段优先按 _raw_indices 回查 raw 置信度（信号 A 在
        # 合并后时间轴复明）；无指针的段（恢复段等）用自身字段。
        if raw_segments and seg.get("_raw_indices") is not None:
            a = signal_a_from_raw(seg, raw_segments,
                                  no_speech_thr=no_speech_thr,
                                  logprob_thr=logprob_thr)
        else:
            a = signal_a_state(seg, no_speech_thr=no_speech_thr,
                               logprob_thr=logprob_thr)

        if energetic and a["suspect"]:
            verdict = MISSING
        elif not energetic and (seg.get("text") or "").strip():
            # B says silence, yet whisper produced text -> hallucination
            # suspicion. We only ever *mark* this (never silently drop a
            # main-pass cue) — dropping is the recovery guard's job.
            verdict = HALLUCINATION
        else:
            verdict = CLEAN

        if verdict == CLEAN:
            continue

        rec: dict[str, Any] = {
            "index": i,
            "start": s,
            "end": e,
            "verdict": verdict,
            "energetic": energetic,
            "reasons": list(a["reasons"]),
            "text": (seg.get("text") or "").strip(),
        }
        if verdict == MISSING:
            rec["g1_windows"] = slice_by_silence(s, e, silences, min_sub=min_sub)
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# G1 — split a suspect window at silence boundaries (ADR-034 §3.2)
# --------------------------------------------------------------------------- #

def slice_by_silence(
    start: float,
    end: float,
    silence_intervals: list[tuple[float, float]] | None = None,
    *,
    min_sub: float = 1.0,
) -> list[tuple[float, float]]:
    """Split [start,end) into the *energetic* pieces bounded by silence.

    G1's whole point is to separate a true utterance from the laughter/noise
    glued to it: the glue is separated by a silence gap, so cutting on silence
    edges yields sub-windows where whisper decodes the real speech without the
    contaminating prefix (ADR-034 §3.2).

    Returns ``[]`` when the window is empty; a single piece when there is no
    silence inside (nothing to cut → G1 cannot help, caller falls through).
    """
    if end <= start:
        return []
    pieces = _subtract([(start, end)], list(silence_intervals or []))
    return [
        (round(a, 2), round(b, 2)) for (a, b) in pieces if (b - a) >= min_sub
    ]


# --------------------------------------------------------------------------- #
# G2 — intra-segment word-uncovered sub-windows (ADR-034 §3.3)
# --------------------------------------------------------------------------- #

def word_coverage(seg: dict[str, Any]) -> list[tuple[float, float]]:
    """Merged word-timestamp coverage of a segment. Pure."""
    spans: list[tuple[float, float]] = []
    for w in (seg.get("words") or []):
        ws, we = w.get("start"), w.get("end")
        if ws is None or we is None:
            continue
        try:
            spans.append((float(ws), float(we)))
        except (TypeError, ValueError):
            continue
    return _merge_intervals(spans)


def find_word_uncovered_subwindows(
    seg: dict[str, Any],
    silence_intervals: list[tuple[float, float]] | None = None,
    *,
    min_dur: float = 1.5,
    min_energy_frac: float = 0.5,
) -> list[tuple[float, float]]:
    """G2 detection: sub-windows *inside* a segment that have energy but no words.

    ADR-034 §3.3 — the classic loss mode: whisper emits one long segment whose
    words cover only part of its span. The timeline is continuous (so a
    segment-gap scan sees nothing wrong) and the character-density collapse test
    misses it whenever the collapsed fragment was short. Here we subtract the
    segment's own word coverage from its span, then subtract detected silence:
    what survives is "audio is sounding here but this segment transcribed
    nothing for it".

    Returns ``[]`` when the segment has no word timestamps (we cannot reason
    about coverage then, and re-decoding a whole segment on suspicion alone would
    be a large, risky rewrite).
    """
    if seg.get("start") is None or seg.get("end") is None:
        return []
    s, e = float(seg["start"]), float(seg["end"])
    if e <= s:
        return []

    words = seg.get("words") or []
    if not words:
        return []

    silences = list(silence_intervals or [])
    # 1) span minus word coverage -> holes between words
    holes = _subtract([(s, e)], word_coverage(seg))
    if not holes:
        return []
    # 2) holes minus detected silence -> only "energy is actually sounding" parts
    energetic = _subtract(holes, silences)

    out: list[tuple[float, float]] = []
    for (a, b) in energetic:
        if (b - a) < min_dur:
            continue
        # Conservative: require the sub-window to be *mostly* non-silent, so we
        # do not re-decode a breath/pause between clauses.
        if not is_energetic(a, b, silences, min_frac=min_energy_frac,
                            min_abs=min_dur * 0.5):
            continue
        out.append((round(a, 2), round(b, 2)))
    return out
