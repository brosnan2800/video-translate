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
# V4: readability floor — cues shorter than this are unreadable and, if they
# are also tiny (few words), get rejoined into their left neighbor.
DEFAULT_MIN_CUE_DUR = 1.0  # seconds
DEFAULT_SHORT_CUE_WORDS = 3
# V6 (B4): a run of at most this many words, separated from the rest of its own
# sentence by at least DEFAULT_DRIFT_GAP seconds, is treated as word-timestamp
# drift rather than a real pause. See snap_drifted_words.
DEFAULT_DRIFT_GAP = 2.0    # seconds
DEFAULT_DRIFT_MAX_RUN = 1  # words
DEFAULT_DRIFT_MAX_DUR = 1.0  # seconds, the drifted run's own span
_SENT_END = re.compile(r"[.!?]\s*$")
_TOKEN_RE = re.compile(r"[a-z0-9']+")


# --------------------- V4: hallucination segment filter ---------------------
#
# WHY: whisper under greedy decoding (and occasionally under beam search)
# emits "hallucination segments" — text with NO acoustic backing. The DTW word
# aligner then cannot place those words, so they collapse onto a single
# timestamp (start == end for most words in the segment). Observed in the wild:
#   "at the end of the day, the movie is a movie"  (8 of 11 words at t=64.99)
# The acoustic invariant is only trustworthy for REAL speech; these segments
# violate it at the source, so we detect and drop them BEFORE merge.
#
# Two signals, BOTH required (each alone is too aggressive):
#   1. collapse: >=50% of the segment's words have zero duration (start>=end)
#   2. repeat:   its text shares a >=3-word consecutive n-gram with a neighbor
# Signal 2 without signal 1 would delete real repeated speech ("I agree. I
# agree."); signal 1 without signal 2 might delete real but poorly-aligned
# speech. Together they are surgically specific to the failure mode.
#
# ADR-012 adds a THIRD, standalone signal for the isolated-silence case the
# dual signal misses: a segment whose entire window lies inside a detected
# silence interval has no acoustic backing (it is phantom text whisper placed
# in silence — e.g. the IF片头 "Hubsan x4" drone model). It has no neighbour to
# share an n-gram with, so signals 1+2 never fire. The independent silencedetect
# reference is the only thing that catches it. Silence == no speech, so dropping
# is safe; it only triggers when `silence_intervals` is supplied.


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _max_shared_ngram(a: list[str], b: list[str]) -> int:
    """Length of the longest common consecutive token run between a and b."""
    if not a or not b:
        return 0
    dp = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        prev = 0
        for j in range(1, len(b) + 1):
            tmp = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev + 1
                if dp[j] > best:
                    best = dp[j]
            else:
                dp[j] = 0
            prev = tmp
    return best


def _collapse_ratio(seg: dict[str, Any]) -> float:
    words = seg.get("words") or []
    if not words:
        return 0.0
    zero = sum(1 for w in words if w.get("start", 0) >= w.get("end", 0))
    return zero / len(words)


def _in_silence_window(start: float, end: float,
                       silences: list[tuple[float, float]],
                       eps: float = 1e-3) -> bool:
    """True when [start, end] lies entirely inside one detected silence interval."""
    for (s0, s1) in silences:
        if start >= s0 - eps and end <= s1 + eps:
            return True
    return False


def drop_hallucination_segments(
    segs: list[dict[str, Any]],
    *,
    min_words: int = 3,
    collapse_ratio: float = 0.5,
    ngram: int = 3,
    silence_intervals: list[tuple[float, float]] | None = None,
    progress=print,
) -> list[dict[str, Any]]:
    """Drop hallucination segments (see module comment above). Pure filter:
    returns a new list; never mutates input. Non-suspect segments pass through
    untouched, preserving order and timestamps.

    ADR-012: when `silence_intervals` is supplied, a segment whose whole window
    sits inside a detected silence interval is dropped as an isolated-silence
    hallucination (no acoustic backing), independently of the collapse/repeat
    dual signal.
    """
    kept: list[dict[str, Any]] = []
    n = len(segs)
    for i, s in enumerate(segs):
        words = s.get("words") or []
        dropped = False
        if len(words) >= min_words and _collapse_ratio(s) >= collapse_ratio:
            toks = _tokens(s.get("text") or "")
            prev = _tokens(segs[i - 1].get("text") or "") if i > 0 else []
            nxt = _tokens(segs[i + 1].get("text") or "") if i + 1 < n else []
            if (_max_shared_ngram(toks, prev) >= ngram
                    or _max_shared_ngram(toks, nxt) >= ngram):
                progress(f"[hallucination] drop seg#{i} "
                         f"({s.get('start')}-{(s.get('end'))}): "
                         f"{(s.get('text') or '')!r} "
                         f"[collapsed {len(words)}w + repeated n-gram]")
                dropped = True
        if not dropped and silence_intervals:
            st = float(s.get("start", 0.0))
            en = float(s.get("end", 0.0))
            if _in_silence_window(st, en, silence_intervals):
                progress(f"[hallucination] drop seg#{i} "
                         f"({st}-{en}): {(s.get('text') or '')!r} "
                         f"[isolated in silence interval — no acoustic backing]")
                dropped = True
        if not dropped:
            kept.append(s)
    return kept


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


# --------------------- V6 (B4): word-timestamp drift snap ---------------------
#
# WHY: Whisper's word timestamps come from DTW over cross-attention — a
# posterior estimate, not an acoustic measurement. Its dominant failure mode is
# pulling a sentence's FIRST word (usually a function word: I / Do / And / So)
# many seconds ahead of the rest of the sentence, typically onto an unrelated
# sound. Two observed in one 64s clip:
#     "I pray you pull back your cavalry..."   I@4.95   pray@17.82  (12.4s)
#     "Do we have terms?"                      Do@32.72 we@40.71    ( 7.6s)
# _split_by_gap then faithfully "preserves" that fake silence and emits an
# orphan cue ("I", "Do") floating seconds before its own sentence — which the
# translator turns into a nonsense single-character subtitle ("可").
#
# This is outlier REJECTION, not timestamp recomputation: a lone word sitting
# >= drift_gap away from the sentence it grammatically belongs to is not an
# alignment fact, and the sentence's own boundary is the better estimate for it.
# Same class of decision as drop_hallucination_segments.
#
# Guards (all required) keep real short utterances intact:
#   1. the isolated run is <= max_run words (default 1)
#   2. the run's own span is < max_dur (a real 3s single word is speech)
#   3. the run does NOT end a sentence — "Yes." after a pause is genuine
#   4. there IS a following group to snap onto (never snap the tail; trailing
#      drift is not a mode DTW exhibits — it lands early, not late)


def snap_drifted_words(
    segs: list[dict[str, Any]],
    *,
    drift_gap: float = DEFAULT_DRIFT_GAP,
    max_run: int = DEFAULT_DRIFT_MAX_RUN,
    max_dur: float = DEFAULT_DRIFT_MAX_DUR,
    progress=print,
) -> list[dict[str, Any]]:
    """Snap drifted leading words forward onto their own sentence.

    Runs BEFORE split_long_cues so the fake gap is gone by the time the splitter
    looks at it. Pure: returns a new list, input is not mutated. Word text and
    order are always preserved; only the drifted run's timestamps move.
    """
    out: list[dict[str, Any]] = []
    for s in segs:
        words = s.get("words") or []
        if len(words) < 2:
            out.append(s)
            continue
        groups = _split_by_gap(words, drift_gap)
        if len(groups) < 2:
            out.append(s)
            continue
        new_words: list[dict[str, Any]] = []
        changed = False
        for gi, g in enumerate(groups):
            nxt = groups[gi + 1] if gi + 1 < len(groups) else None
            span = g[-1]["end"] - g[0]["start"]
            ends_sent = _SENT_END.search(_joined(g)) is not None
            if (nxt is not None and len(g) <= max_run and span < max_dur
                    and not ends_sent):
                anchor = nxt[0]["start"]
                progress(f"[drift] snap {_joined(g)!r} "
                         f"{g[0]['start']:.2f}->{anchor:.2f} "
                         f"(was {anchor - g[-1]['end']:.2f}s ahead of its sentence)")
                new_words.extend(
                    {**w, "start": anchor, "end": anchor} for w in g
                )
                changed = True
            else:
                new_words.extend(g)
        if not changed:
            out.append(s)
            continue
        seg = dict(s)
        seg["words"] = new_words
        seg["start"] = new_words[0]["start"]
        seg["end"] = new_words[-1]["end"]
        out.append(seg)
    return out


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


# --------------------- V4: short-cue rejoin (readability) ---------------------
#
# WHY: _split_by_gap faithfully preserves real pauses, but its by-products are
# orphan cues like "coding" (0.2s), "love?" (0.4s) — unreadable. Rejoin them
# into the LEFT neighbor when ALL of these hold:
#   1. cue is a fragment: duration < min_dur AND <= max_words words
#   2. left neighbor exists and is close (gap < 1.0s — a bigger silence means
#      the fragment is genuinely standalone)
#   3. left neighbor does NOT end a sentence ([.!?]) — joining "Resurrection."
#      with a drifted "I've" would corrupt both semantics and alignment
#   4. joined span stays <= max_dur
# Everything else is left as-is (generate's --min-dur extends the DISPLAY
# window for the survivors, so readability is still guaranteed downstream).


def merge_short_cues(
    segs: list[dict[str, Any]],
    *,
    min_dur: float = DEFAULT_MIN_CUE_DUR,
    max_words: int = DEFAULT_SHORT_CUE_WORDS,
    max_dur: float = DEFAULT_MAX_DUR,
) -> list[dict[str, Any]]:
    """Rejoin unreadable split by-products into their left neighbor. Pure:
    returns a new list; input not mutated. Boundaries come only from inputs
    (first start / last end), words are concatenated — the acoustic invariant
    is preserved (the inter-cue silence simply lives inside the joined window).
    """
    out: list[dict[str, Any]] = []
    for s in segs:
        dur = s["end"] - s["start"]
        nwords = len(s.get("words") or [])
        is_fragment = dur < min_dur and (nwords <= max_words if nwords else True)
        if (is_fragment and out
                and s["start"] - out[-1]["end"] < 1.0
                and not _SENT_END.search((out[-1].get("text") or "").strip())
                and s["end"] - out[-1]["start"] <= max_dur):
            left = out[-1]
            left_words = left.get("words") or []
            s_words = s.get("words") or []
            merged: dict[str, Any] = {
                "start": left["start"],
                "end": s["end"],
                "text": ((left.get("text") or "").strip() + " "
                         + (s.get("text") or "").strip()).strip(),
            }
            if left_words or s_words:
                merged["words"] = left_words + s_words
            out[-1] = merged
        else:
            out.append(dict(s))
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
    drop_hallucinations: bool = True,
    snap_drift: bool = True,
    drift_gap: float = DEFAULT_DRIFT_GAP,
    rejoin_short: bool = True,
    silence_intervals: list[tuple[float, float]] | None = None,
    progress=print,
) -> str:
    """Pipeline hook: read `segments_path` (raw), save a copy to `raw_path`,
    filter hallucinations, merge, split, rejoin short cues, and overwrite
    `segments_path` with the result.

    V6 order (each stage's output is the next stage's input):
      0. drop_hallucination_segments — collapsed+repeated whisper artifacts
      1. merge_segments               — rejoin whisper's own fragmentation
      2. snap_drifted_words           — kill fake gaps from DTW drift (B4)
      3. split_long_cues              — word-level split at real silences / 剪映
      4. merge_short_cues             — rejoin the split's unreadable orphans

    Stage 2 must precede stage 3: the splitter cannot tell a fake gap from a
    real pause, so the fake ones have to be gone before it runs.

    Returns segments_path. Used by the CLI after transcribe; `--no-merge` skips
    this entirely; `--no-split` keeps merge but disables cue splitting.
    """
    raw = load_json(segments_path)
    save_json(raw_path, raw, indent=0)
    if drop_hallucinations:
        before = len(raw)
        raw = drop_hallucination_segments(
            raw, silence_intervals=silence_intervals, progress=progress)
        dropped = before - len(raw)
        if dropped:
            progress(f"[hallucination] dropped {dropped} segment(s) "
                     f"(raw kept at {raw_path})")
    merged = merge_segments(
        raw, max_dur=max_dur, max_gap=max_gap,
        respect_sentence_end=respect_sentence_end,
    )
    if snap_drift:
        merged = snap_drifted_words(merged, drift_gap=drift_gap, progress=progress)
    if split_enabled:
        merged = split_long_cues(
            merged, max_chars=split_max_chars, max_gap=split_max_gap, enabled=True,
        )
    if rejoin_short:
        merged = merge_short_cues(merged, max_dur=max_dur)
    save_json(segments_path, merged, indent=0)
    return segments_path
