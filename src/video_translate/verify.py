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

import re
from typing import Any

# Issue types
IN_SILENCE = "in-silence"
CROSS_SILENCE = "cross-silence"
FIRST_CUE_EARLY = "first-cue-early"
TAIL_STRIPPED = "tail-stripped"
MIN_DUR_STRIPPED = "min-dur-stripped"
UNCOVERED_AUDIO = "uncovered-audio"
LOW_CONFIDENCE = "low-confidence"        # ADR-031 D3: whisper 自判非语音/低置信
ADJACENT_OVERLAP = "adjacent-overlap"    # ADR-031 D4: 相邻段声学窗口重叠

# ADR-031 D7: BGM/语音能量分级阈值（kathy_meta_vlog 人声轨实测标定，
# 与轮 2 人工仲裁同标准）。确认语音窗人声轨 mean ≈ -15..-20dB / max ≈ -2..-6dB；
# BGM 残响窗 mean ≤ -36dB。
BGM_MEAN_DB = -36.0
SPEECH_MEAN_DB = -25.0
SPEECH_MAX_DB = -12.0

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


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Union-merge a list of [start, end) intervals (sorted, deduped)."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[list[float]] = [[float(ordered[0][0]), float(ordered[0][1])]]
    for (s, e) in ordered[1:]:
        s, e = float(s), float(e)
        if s <= merged[-1][1] + _EPS:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def find_uncovered_speech(
    segments: list[dict[str, Any]],
    silence_intervals: list[tuple[float, float]],
    duration: float,
    min_dur: float = 2.0,
) -> list[tuple[float, float]]:
    """Return timeline stretches with audio present but NO cue (ADR-016 / T2b).

    A missing segment leaves no cue, so the existing acoustic lane (which only
    inspects cues against silence) never flags it. This detection pass works the
    other way: compute the complement of all cue coverage, then subtract the
    detected silence — what remains is "audio present but unsubtitled".

    Args:
        segments: cue list (each with ``start``/``end``).
        silence_intervals: (start, end) silence intervals from silencedetect.
        duration: media duration in seconds.
        min_dur: only flag uncovered-audio stretches at least this long.

    Returns a list of ``(start, end)`` uncovered-audio windows. Pure (no I/O).
    """
    if duration is None or duration <= 0:
        return []
    coverage = [
        (float(s["start"]), float(s["end"]))
        for s in segments
        if s.get("start") is not None and s.get("end") is not None
    ]
    merged = _merge_intervals(coverage)
    # 1) complement of coverage over [0, duration] -> uncovered stretches
    uncovered: list[tuple[float, float]] = []
    cursor = 0.0
    for (s, e) in merged:
        if s > cursor + _EPS:
            uncovered.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration - _EPS:
        uncovered.append((cursor, duration))
    # 2) subtract silence -> keep only "audio present" sub-stretches
    out: list[tuple[float, float]] = []
    for (g0, g1) in uncovered:
        pieces: list[tuple[float, float]] = [(g0, g1)]
        for (s0, s1) in silence_intervals:
            s0 = max(float(s0), g0)
            s1 = min(float(s1), g1)
            if s1 <= s0:
                continue
            carved: list[tuple[float, float]] = []
            for (p0, p1) in pieces:
                if s0 >= p1 or s1 <= p0:
                    carved.append((p0, p1))
                else:
                    if p0 < s0:
                        carved.append((p0, s0))
                    if s1 < p1:
                        carved.append((s1, p1))
            pieces = carved
        for (p0, p1) in pieces:
            if (p1 - p0) >= min_dur:
                out.append((round(p0, 2), round(p1, 2)))
    return out


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


def is_recovered_segment(seg: dict[str, Any]) -> bool:
    """True for fill_gaps-recovered or resegment-spliced segments (ADR-031 D2).

    Recovered segments carry a structurally higher hallucination risk (they are
    force-decoded onto hole/BGM energy), so downstream review (verify report,
    semantic reread task) must be able to SEE them. fill_gaps tags its
    recoveries with ``_recovered: True``; resegment tags its splices with
    ``origin: "resegment"`` (ADR-031 D1/D2).
    """
    if seg.get("_recovered"):
        return True
    origin = seg.get("origin")
    return origin is not None and origin != "whisper"


def find_low_confidence_segments(
    segments: list[dict[str, Any]],
    *,
    no_speech_thr: float = 0.6,
    logprob_thr: float = -1.0,
) -> list[dict[str, Any]]:
    """Flag segments Whisper itself scored as non-speech / low confidence (D3).

    Same thresholds as the fill_gaps recovery guard (ADR-021: no_speech_prob
    >= 0.6 is the STRONGEST single hallucination signal; avg_logprob < -1.0 as
    the fallback). kathy_meta_vlog delivered "We'll be right back." (nsp=0.906)
    and "Wait." (nsp=0.851) because nothing downstream ever re-checked the
    stored confidence fields — this inspection closes that blind spot for BOTH
    new and existing timelines. Segments without confidence fields (main-pass
    segments after merge) are skipped.

    Pure (no I/O). Returns issue dicts: ``{index, type, no_speech_prob?,
    avg_logprob?, start?, end?}``.
    """
    issues: list[dict[str, Any]] = []
    for i, s in enumerate(segments):
        hits: dict[str, float] = {}
        nsp = s.get("no_speech_prob")
        if nsp is not None and float(nsp) >= no_speech_thr:
            hits["no_speech_prob"] = float(nsp)
        alp = s.get("avg_logprob")
        if alp is not None and float(alp) < logprob_thr:
            hits["avg_logprob"] = float(alp)
        if hits:
            issues.append({"index": i, "type": LOW_CONFIDENCE,
                           "start": s.get("start"), "end": s.get("end"), **hits})
    return issues


def find_adjacent_overlaps(
    segments: list[dict[str, Any]],
    *,
    min_overlap: float = 0.05,
    word_eps: float = 0.02,
) -> list[dict[str, Any]]:
    """Flag adjacent cues whose acoustic windows overlap (ADR-031 D4/D5).

    Two utterances cannot occupy the same wall-clock audio: an overlap beyond
    ordinary fuzzy boundaries (>0.05s) means one of the two windows is wrong —
    typically a recovery riding on confirmed neighbour audio. When the later
    cue's FIRST word starts before the earlier cue's LAST word ends, the
    overlap is word-level (``word_collision``); for recovered segments that is
    the classic hallucinated-prefix fingerprint (kathy_meta_vlog: the "Is" of
    "Is he not going to make it?" riding on "busy."), so the issue carries a
    ``hint``. Report-only: timestamps are never auto-trimmed (ADR-012 red
    line) — the fix is resegment/manual, not silent rewriting.

    Pure (no I/O). Returns issue dicts sorted by cue order.
    """
    issues: list[dict[str, Any]] = []
    order = sorted(range(len(segments)),
                   key=lambda i: float(segments[i].get("start") or 0.0))
    for pos_b, b in enumerate(order):
        sb = float(segments[b].get("start") or 0.0)
        best: tuple[int, float] | None = None
        for a in order[:pos_b]:
            ea = float(segments[a].get("end") or 0.0)
            ov = ea - sb
            if ov <= min_overlap:
                continue
            if best is None or ov > best[1]:
                best = (a, ov)
        if best is None:
            continue
        a, ov = best
        issue: dict[str, Any] = {
            "index": b, "index_a": a, "index_b": b, "type": ADJACENT_OVERLAP,
            "start": sb, "end": float(segments[a].get("end") or 0.0),
            "overlap": round(ov, 3),
        }
        wa = segments[a].get("words") or []
        wb = segments[b].get("words") or []
        ws0 = wb[0].get("start") if wb else None
        we_last = wa[-1].get("end") if wa else None
        if (ws0 is not None and we_last is not None
                and float(ws0) < float(we_last) - word_eps):
            issue["word_collision"] = True
            if is_recovered_segment(segments[b]):
                issue["hint"] = ("suspected hallucinated prefix: first word(s) "
                                 "of the recovered segment ride on neighbour "
                                 "audio")
        issues.append(issue)
    return issues


def classify_vocals_energy(mean_db: float | None, max_db: float | None) -> str:
    """Classify a window's vocal-track energy: bgm / speech / ambiguous / unknown.

    Thresholds calibrated on kathy_meta_vlog (ADR-031 D7): confirmed speech
    windows sit at mean ≈ -15..-20dB / max ≈ -2..-6dB on the demucs vocals
    track; BGM residue sits at mean ≤ -36dB (same standard as the round-2
    manual adjudication). Pure (no I/O).
    """
    if mean_db is None or max_db is None:
        return "unknown"
    if mean_db <= BGM_MEAN_DB:
        return "bgm"
    if mean_db >= SPEECH_MEAN_DB or max_db >= SPEECH_MAX_DB:
        return "speech"
    return "ambiguous"


_VOCALS_SUGGESTIONS = {
    "bgm": "likely BGM residue — adjudicate as music bed, no resegment needed",
    "speech": "vocal energy present — resegment this window (guard is built in)",
    "ambiguous": "mid energy — listen and adjudicate manually",
    "unknown": "vocals-track probe failed — adjudicate manually",
}


def classify_uncovered_windows(
    uncovered: list[tuple[float, float]],
    volumes: dict[tuple[float, float], tuple[float | None, float | None]],
) -> dict[tuple[float, float], dict[str, str]]:
    """Attach a per-window BGM/speech verdict to uncovered windows (D7).

    ``volumes`` maps each uncovered window to a ``(mean_db, max_db)`` probe
    result on the vocals track (missing entries / None values -> "unknown").
    Pure (no I/O) — the caller (cli) does the probing.
    """
    out: dict[tuple[float, float], dict[str, str]] = {}
    for w in uncovered:
        verdict = classify_vocals_energy(*volumes.get(w, (None, None)))
        out[w] = {"verdict": verdict, "suggestion": _VOCALS_SUGGESTIONS[verdict]}
    return out


def find_untranslated_latin_words(zh_text: str) -> list[str]:
    """Return lower-case latin tokens left untranslated inside a zh subtitle.

    A leftover *lower-case* latin word is almost always a missed translation
    (e.g. ``"rivalry"`` kept as-is). Upper-case tokens (``OK``/``AI``) and
    title-case tokens (``Ken``/``Barbenheimer`` — proper nouns legitimately kept)
    are NOT flagged. This is a deterministic content-layer check that catches the
    "中英混杂" class the semantic reread may glance over. Pure (no I/O); may emit
    false positives on borrowed words (``app``/``rap``/``pop``) — verify only
    flags, the agent confirms.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z']*", zh_text or "")
    return [t for t in tokens if len(t) > 1 and not t.isupper() and not t[0].isupper()]


def build_semantic_reread_task(
    segments: list[dict[str, Any]],
    zh: dict[int, str],
    *,
    window: int = 3,
) -> dict[str, Any]:
    """Build an agent-side semantic reread task (ADR-005: CLI never calls an LLM).

    The task lists every segment's English source + its Chinese translation plus
    a neighbour context window (English AND Chinese neighbours), so the *calling
    agent* (which has LLM access) can reread each pair in context and flag
    omissions / additions / mistranslations / cross-sentence inconsistency
    (referents, tone, terminology) — the third content-layer check
    (`validate_zh` covers coverage, `verify_align` covers index drift; this covers
    fidelity). Returns a dict ready to be serialised to
    ``<base>.semantic_reread_task.json``.
    """
    pairs: list[dict[str, Any]] = []
    n = len(segments)
    for i, s in enumerate(segments):
        idx = i  # zh is 0-based keyed by segment position
        en = (s.get("text") or "").strip()
        cn = (zh.get(idx) or "").strip()
        if not en and not cn:
            continue
        before = range(max(0, i - window), i)
        after = range(i + 1, min(n, i + 1 + window))
        context_before = [(segments[j].get("text") or "").strip() for j in before]
        context_after = [(segments[j].get("text") or "").strip() for j in after]
        # 中文邻居译文：判断"这句中文对不对"常需看相邻句的中文是否连贯
        # （指代一致、术语统一、语气连贯），故把 zh 邻居一并带上。
        zh_before = [(zh.get(j) or "").strip() for j in before]
        zh_after = [(zh.get(j) or "").strip() for j in after]
        pair: dict[str, Any] = {
            "index": idx,
            "en": en,
            "zh": cn,
            "context_before": context_before,
            "context_after": context_after,
            "context_before_zh": zh_before,
            "context_after_zh": zh_after,
        }
        # ADR-031 D2: recovered segments (fill_gaps / resegment) carry a
        # structurally higher hallucination risk. Semantic plausibility is NOT
        # evidence — whisper hallucinations are precisely "the most plausible
        # line for this context" ("We'll be right back.", nsp=0.906). The task
        # must tell the rereading agent to demand acoustic evidence.
        if is_recovered_segment(s):
            pair["suspect"] = True
            pair["hint"] = ("recovered segment (fill_gaps/resegment): high "
                            "hallucination suspicion — confirm against audio/"
                            "energy evidence, not just semantic plausibility")
        pairs.append(pair)
    return {
        "task": "semantic-reread",
        "persona": "You are a rigorous translator. Re-read each (en, zh) pair "
                   "WITH its context_before/after (English) and "
                   "context_before_zh/after_zh (Chinese) and flag any omission, "
                   "addition, mistranslation, or cross-sentence inconsistency "
                   "(referents, tone, terminology). Output only the indices that "
                   "are wrong and why.",
        "pairs": pairs,
        "output_schema": {
            "<index>: <'ok' | 'omit' | 'add' | 'wrong' | 'untranslated': reason>": "..."
        },
    }
