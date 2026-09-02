"""B: coverage self-audit + automatic gap recovery.

Even after transcription with ``no_speech_threshold=0`` (see transcribe.py),
whisper can still drop audible speech — most often stylized / sung / impression
audio it scores as "non-speech", or a chunk-edge artifact. This module scans
the segment timeline for *audible holes* and force-decodes each one:

  * HEAD  — the silence before the first segment (0.0 -> seg[0].start)
  * TAIL  — the silence after the last segment (seg[-1].end -> video duration)
  * interior — the gap between two adjacent segments

Each hole is re-decoded with ``no_speech_threshold=0`` (so silence is never
auto-suppressed). Recovered text that merely *echoes* a neighbouring segment
(whisper's decoder leaking the adjacent line into the quiet gap) is dropped;
genuinely new speech is spliced back into the timeline. The output is a
complete, monotonic segment list.

A *fourth* loss mode has no time hole at all: whisper emits one segment whose
timespan covers many seconds but whose text holds only a fragment — the rest of
the speech is silently collapsed away. Example from the wild::

    seg  6   1.66s  cps=22.9  "...are the moments where Jamie proved his"
    seg  7  13.12s  cps= 3.4  "his range is basically a superpower, put two"   <-- 10s lost
    seg  8   2.58s  cps=18.6  "legendary actors in the same room and eventually"

The timeline is continuous, so a gap scan sees nothing wrong. We detect these by
*character density* (chars per second) against the file's own median: a long
segment whose density is a small fraction of the median is a collapse. Its
window is re-decoded and, when the decode yields materially more speech, the
collapsed segment is **replaced** by the recovered ones.

This is the production version of the manual rescue flow; it differs from the
early /tmp script in three important ways: it audits HEAD and TAIL holes (the
manual version only checked interior gaps, which is how the opening Tyson
impression got lost), it detects in-segment collapse (no time hole), and it
deduplicates against *every* existing segment, not just the immediate neighbours.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import statistics
import tempfile
from typing import Any

from .ffmpeg_utils import extract_chunk, probe_duration
from .audio_profile import (
    CLEAN_SILENCE_FRACTION,
    probe_window_silence_fraction,
)
from .review import (
    MISSING,
    find_word_uncovered_subwindows,
    review_segments,
)
from .transcribe import (
    BEAM_SIZE, BEST_OF, resolve_device,
    CONDITION_ON_PREVIOUS_TEXT, REPETITION_PENALTY,
    NO_SPEECH_THRESHOLD, TEMPERATURE_FALLBACK,
)


# Pads (seconds) tried when decoding a suspect window, in order. A small pad
# avoids dragging the neighbouring line's tail into the decoder's prompt (which
# triggers whisper's prefix collapse); the larger fallbacks exist for windows
# whose true speech starts slightly before the recorded boundary.
_PROBE_PADS: tuple[float, ...] = (0.2, 0.0, 0.5)
# Only long windows are worth multi-probing; short holes get a single decode.
_MULTI_PROBE_MIN_WINDOW = 4.0
# Stop probing once a decode covers this fraction of the window.
_PROBE_GOOD_COVERAGE = 0.6

# Long-hole sub-windowing (recall hardening, B direction). A single forced
# decode over a very wide hole (e.g. a 40s gap) is unreliable — whisper tends to
# collapse it into one fragment or hallucinate, and the pad rotation above
# cannot rescue it. We instead slice the hole into sub-windows of at most
# ``_SUBWIN`` seconds (with ``_SUBWIN_OVERLAP`` overlap to avoid clipping a
# sentence straddling a cut) and decode each independently, then de-duplicate the
# seams. This is what recovers the speech hidden inside large uncovered-audio
# windows that verify's acoustic lane flags (ADR-016 T2b).
_SUBWIN = 12.0
_SUBWIN_OVERLAP = 0.5


def _norm_tokens(s: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


def _jaccard(a: str, b: str) -> float:
    A, B = _norm_tokens(a), _norm_tokens(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _overlap_with_any(cand: dict[str, Any],
                       segments: list[dict[str, Any]]) -> float:
    """Absolute time (seconds) that `cand`'s window overlaps any segment in
    `segments`. Zero when isolated (the expected case for genuine recovery
    spliced into a real hole)."""
    cs, ce = float(cand["start"]), float(cand["end"])
    if ce <= cs:
        return 0.0
    best = 0.0
    for seg in segments:
        ss, se = float(seg["start"]), float(seg["end"])
        ov = min(ce, se) - max(cs, ss)
        if ov > best:
            best = ov
    return best


def _recovered_wps(cand: dict[str, Any]) -> float:
    """speaking rate of a recovered segment: words / duration (words/sec)."""
    words = cand.get("words") or []
    dur = float(cand["end"]) - float(cand["start"])
    if dur <= 0.01 or not words:
        return 0.0
    return len(words) / dur


def _is_recovered_hallucination(
    cand: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    overlap_eps: float = 0.12,
    max_wps: float = 8.0,
    min_words: int = 2,
    max_words_for_overlap: int = 4,
    avg_logprob_thr: float = -1.0,
    no_speech_thr: float = 0.6,
    min_zero_dur_words: int = 2,
    check_overlap: bool = True,
) -> bool:
    """Hallucination guard for fill_gaps RECOVERED segments (ADR-020 addendum).

    Recovered segments are force-decoded to fill time holes. They ONLY pass the
    text-similarity ``_is_echo`` check today; timestamp geometry is ignored, so
    "audio-sharing" phantoms (whisper re-emitting a line that rides on the
    already-confirmed neighbour audio) slip into the timeline. jimmy.mp4 showed
    7 such cases (``Don't worry.``, ``I'm fucking fired!``, ``Субтитры...``,
    ``I'm a clown.``, ``Hi, son.``, ``Now what?``, ``This is bad.``).

    A recovered segment's job is to fill a hole (the gap between existing
    segments). If its window overlaps an existing segment's window, it is
    decoding the already-confirmed audio — an audio-sharing echo (signal A).
    BUT genuine adjacent cues have fuzzy boundaries (a few hundred ms) without
    nesting — e.g. ``anxious. There's a difference.`` overlaps the prior
    ``...you get anxi[ous]`` by 0.20s yet is real speech. So signal A only
    fires for SHORT recovered segments (<= max_words_for_overlap words): a long
    recovered line is a real sentence regardless of a small boundary overlap.

    Signals (any hit => hallucination), all conservative to avoid dropping real
    recovered speech:
      A. words <= max_words_for_overlap AND overlaps any existing segment by >
         overlap_eps  (0.12s; short phantoms overlap >=0.16s, real long lines
         exempt)
      B. words >= min_words AND speaking rate (wps) > max_wps  (physically
         impossible rate, e.g. 3 words in 0.16s = 18.8 wps)
      C. Whisper non-speech judgement: no_speech_prob >= no_speech_thr
         (0.6) — the STRONGEST single signal for recovered segments. fill_gaps
         force-decodes holes with no_speech_threshold=0, so a recovery the
         model scores >=60% non-speech is energy, not speech; its text is a
         hallucination. avg_logprob must NOT gate this (phantoms can score
         -0.65 yet be 76% non-speech). C2: avg_logprob < thr alone as a
         fallback for older caches that lack no_speech_prob.
      D. >= min_zero_dur_words words with zero duration (start >= end) — the
         DTW-collapse fingerprint (ADR-020 signal 4) applied to RECOVERED
         segments. A phantom decoded from non-speech energy (opening drone
         boot / ambient hum) often contains words the aligner cannot place, so
         they collapse onto a single timestamp. Real recovered speech keeps
         real word intervals (0 zero-duration words in every real fixture);
         e.g. kathy_meta_vlog's ``Hubsan x4 H502E Desire 2-3-18`` phantom has
         three zero-duration tail words ("2", "-3", "-18") while the genuine
         ``We'll be right back.`` / ``Get it.`` / ``Thank you.`` recoveries
         have none.  This closes the A/B/C blind spot: that phantom had no
         overlap, 0.66 wps and avg_logprob -0.942 (> -1.0 thr).

    `check_overlap` is set False on the collapse-replacement path, where the
    recovered window is *expected* to overlap the replaced segment — disabling
    signal A there prevents dropping genuinely recovered replacement speech.
    """
    nw = len(cand.get("words") or [])
    if (check_overlap and nw < max_words_for_overlap
            and _overlap_with_any(cand, segments) > overlap_eps):
        return True
    if (cand.get("words") and nw >= min_words
            and _recovered_wps(cand) > max_wps):
        return True
    # D: zero-duration words (DTW collapse fingerprint). Fire before C so a
    # phantom with zero-dur words is dropped even when its avg_logprob sits just
    # above the -1.0 threshold (the Hubsan blind spot).
    if (cand.get("words")
            and _count_zero_dur(cand) >= min_zero_dur_words):
        return True
    # C: Whisper's own non-speech judgement. fill_gaps force-decodes holes
    # with no_speech_threshold=0, so a recovery carrying high no_speech_prob
    # means the model heard energy but not clear speech — the recovered text
    # is overwhelmingly likely a hallucination. no_speech_prob is the STRONGEST
    # single signal for recovered segments; avg_logprob must NOT gate it:
    #   Hubsan phantom       0.642 -> dropped (also via D)
    #   We'll be right back. 0.766 -> dropped (signal A misses: exactly 4
    #                                  words; old AND never fired at -0.65)
    #   Thank you.           0.799 -> dropped
    #   Get it.              0.373 -> kept  (low no_speech, plausible)
    # Real recovered speech keeps low no_speech_prob (jimmy 'Got it walking.'
    # 0.1). Threshold mirrors the old no_speech_thr (0.6).
    nsp = cand.get("no_speech_prob")
    if nsp is not None and nsp >= no_speech_thr:
        return True
    # C2: very low avg_logprob alone (older caches may lack no_speech_prob).
    alp = cand.get("avg_logprob")
    if alp is not None and alp < avg_logprob_thr:
        return True
    return False


def _count_zero_dur(cand: dict[str, Any]) -> int:
    """Count words whose interval collapsed to zero duration (start >= end).

    Mirrors merge.py's ``_collapse_ratio`` primitive: a word the DTW aligner
    cannot place collapses onto a single timestamp — the geometric fingerprint
    of a hallucinated token over non-speech energy (ADR-020 signal 4).
    """
    return sum(1 for w in (cand.get("words") or [])
               if w.get("start", 0) >= w.get("end", 0))


def _is_echo(text: str, segments: list[dict[str, Any]]) -> bool:
    """True if `text` is a leak of an already-present segment (not new speech).

    Heuristics, in increasing cost: too short to be real speech; exact string
    containment in either direction; high token overlap (Jaccard > 0.6); high
    character-level similarity (SequenceMatcher > 0.7).

    The character-level test matters because whisper transcribes the *same*
    utterance differently on either side of a boundary — ``"he bit my earl."``
    vs ``"He bit my ear off."`` scores only 0.5 on Jaccard (below the token
    threshold) yet 0.78 on characters, and without it the same line lands in the
    subtitle two or three times.
    """
    t = (text or "").strip()
    if len(t.split()) < 2:
        return True  # whisper sometimes emits a single filler word in silence
    tl = t.lower()
    for seg in segments:
        et = (seg.get("text") or "").strip().lower()
        if not et:
            continue
        if tl in et or et in tl:
            return True
        if _jaccard(t, et) > 0.6:
            return True
        if _ratio(t, et) > 0.7:
            return True
    return False


def _text_sim(a: str, b: str) -> float:
    """Similarity for seam de-duplication: Jaccard, falling back to char ratio.

    Reuses the same notion of "same utterance transcribed differently across a
    boundary" that ``_is_echo`` relies on, but returns a continuous score so the
    caller can threshold it.
    """
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    j = _jaccard(a, b)
    if j > 0:
        return j
    return _ratio(a, b)


def _dedupe_seams(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate fragments produced at sub-window seams.

    Two recovered segments are a seam duplicate when their time ranges overlap
    and their text is near-identical (same neighbour tail decoded by two adjacent
    sub-windows). We keep the earlier-starting one and trim any still-overlapping
    later fragment to avoid double subtitles.
    """
    if not items:
        return []
    items = sorted(items, key=lambda s: float(s["start"]))
    out: list[dict[str, Any]] = []
    for it in items:
        ts, te = float(it["start"]), float(it["end"])
        dup = False
        for kept in reversed(out):
            ks, ke = float(kept["start"]), float(kept["end"])
            overlap = min(te, ke) - max(ts, ks)
            if overlap > 0.2 and _text_sim(it["text"], kept["text"]) > 0.5:
                dup = True  # same fragment decoded by an adjacent sub-window
                break
            if overlap > 0.0:  # trim residual overlap, keep earlier window
                te = min(te, ks)
        if not dup and te > ts:
            out.append({**it, "start": round(ts, 2), "end": round(te, 2)})
    return out


def _slice_long_hole(gs: float, ge: float) -> list[tuple[float, float]]:
    """Sub-window boundaries for a wide hole (module-level for testing)."""
    step = _SUBWIN - _SUBWIN_OVERLAP
    subwins: list[tuple[float, float]] = []
    cur = gs
    while cur < ge - _EPS:
        subwins.append((cur, min(cur + _SUBWIN, ge)))
        cur += step
    return subwins


def _cps(seg: dict[str, Any]) -> float:
    dur = float(seg["end"]) - float(seg["start"])
    return len((seg.get("text") or "").strip()) / max(dur, 0.01)


_EPS = 1e-3


# --------------------------------------------------------------------------- #
# ADR-034 §5.2 — independent review cache layer
# --------------------------------------------------------------------------- #
# review + G1/G2 results live in their OWN cache, keyed by a digest of the
# timeline they were computed from. Exactly like the align pass, this cache
# never enters ``transcribe_fingerprint``: toggling review must NOT invalidate
# (or silently re-trigger) the bare transcription pass, and a re-run must only
# re-process suspect windows instead of re-decoding the whole video.
REVIEW_CACHE_SCHEMA = 1


def review_cache_path(outdir: str, base: str) -> str:
    """Path of the independent review cache for one video."""
    return os.path.join(outdir, f"{base}.review.json")


def review_digest(segments: list[dict[str, Any]]) -> str:
    """Stable digest of the timeline the review ran on.

    Any change to the segment list (re-transcription, merge tweak, resegment,
    fill_gaps recovery) changes the digest, so a stale cache can never splice
    recoveries onto a different timeline.
    """
    h = hashlib.sha1()
    h.update(json.dumps(segments, sort_keys=True, default=str,
                        ensure_ascii=False).encode("utf-8"))
    return "sha1:" + h.hexdigest()


def _load_review_cache(path: str, digest: str,
                       params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return the cached ``merged`` timeline, or None on any miss/mismatch."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:  # noqa: BLE001 - a corrupt cache is just a miss
        return None
    if not isinstance(blob, dict):
        return None
    if blob.get("schema") != REVIEW_CACHE_SCHEMA:
        return None
    if blob.get("segments_digest") != digest:
        return None
    if blob.get("params") != params:
        return None
    merged = blob.get("merged")
    return merged if isinstance(merged, list) else None


def _save_review_cache(path: str, digest: str, params: dict[str, Any],
                       merged: list[dict[str, Any]],
                       g3_unrecoverable: list[dict[str, Any]] | None = None
                       ) -> None:
    """Best-effort persistence. The cache is an optimisation — never fail the
    pipeline (or change its exit code) because it could not be written.

    ``g3_unrecoverable`` 落盘供人工复盘（用户翻产物文件看结果，不看 stdout）。
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": REVIEW_CACHE_SCHEMA,
                       "segments_digest": digest,
                       "params": params,
                       "merged": merged,
                       "g3_unrecoverable": list(g3_unrecoverable or [])},
                      fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _hole_in_silence(gs: float, ge: float,
                     silences: list[tuple[float, float]]) -> bool:
    """True when a hole window lies entirely inside a detected silence interval.

    ADR-012: a hole that is *genuine* silence (intro/outro/pause) must NOT be
    force-decoded — doing so is exactly how isolated hallucinations (e.g. the
    IF片头 "Hubsan x4" drone model) get "recovered" into the timeline.
    """
    for (s0, s1) in silences:
        if gs >= s0 - _EPS and ge <= s1 + _EPS:
            return True
    return False


def _profile_silences(input_path: str, noise: str, d: float) -> list[tuple[float, float]]:
    """Best-effort silence intervals via the independent silencedetect reference.

    Returns [] on any failure so callers fall back to the legacy behaviour.
    """
    try:
        from .audio_profile import analyze_audio
        prof = analyze_audio(input_path, noise=noise, d=d)
        return prof.silence_intervals if prof.ok else []
    except Exception:  # noqa: BLE001
        return []


def _probe_silences_or_none(
    input_path: str, noise: str, d: float,
) -> list[tuple[float, float]] | None:
    """silencedetect reference, or None when the probe could not run at all.

    Distinct from ``_profile_silences`` (which collapses "probe failed" and
    "no silence found" into the same ``[]``): the dual-signal review needs to
    know whether signal B is genuinely AVAILABLE, because B is the main judge.
    An empty-but-successful result means "no silence anywhere" (every window is
    energetic) — the opposite of "we have no idea".
    """
    try:
        from .audio_profile import analyze_audio
        prof = analyze_audio(input_path, noise=noise, d=d)
        return list(prof.silence_intervals) if prof.ok else None
    except Exception:  # noqa: BLE001
        return None


def find_collapsed(
    segments: list[dict[str, Any]],
    *,
    min_dur: float = 4.0,
    ratio: float = 0.45,
) -> list[int]:
    """Indices of segments that look like an in-segment collapse.

    A collapse is a *long* segment (>= `min_dur`) whose character density is
    below `ratio` x the file's own median density. Using the file's own median
    (rather than an absolute cps threshold) keeps the test robust across
    speakers, languages and speaking rates.
    """
    dens = [_cps(s) for s in segments if float(s["end"]) - float(s["start"]) >= 0.8]
    if len(dens) < 5:
        return []
    med = statistics.median(dens)
    if med <= 0:
        return []
    cutoff = med * ratio
    return [
        i for i, s in enumerate(segments)
        if (float(s["end"]) - float(s["start"])) >= min_dur and _cps(s) < cutoff
    ]


# --------------------------------------------------------------------------- #
# ADR-034 §6.3 — G3 局部 separate-vocals：四组进入条件（纯函数，全部可单测）
# --------------------------------------------------------------------------- #
# G3 只兜「强 BGM / 连续噪声掩盖真音」；**不兜笑声**（笑声是人声，demucs 分不
# 开，属能力边界外，ADR-034 §1.4）。四组门控全部是纯函数，demucs 侧全部 mock
# 即可覆盖，见 tests/test_g3_entry.py。

def group_short_windows(
    windows: list[tuple[float, float]],
    *,
    min_dur: float = 5.0,
    max_merge_gap: float = 2.0,
) -> list[tuple[float, float]]:
    """组1：把 < min_dur 的短窗与邻近可疑窗合并后一起分离。

    demucs 在 <5s 窗上质量很差（ADR-034 §3.4 组1），所以短窗不单独分离，而是
    与间隔 <= max_merge_gap 的相邻可疑窗并成一组；已 >= min_dur 的窗不强行拖
    邻居进来（避免把干净语音卷进分离）。
    """
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: (float(w[0]), float(w[1])))
    groups: list[list[float]] = []          # [start, end]
    for (s, e) in ordered:
        s, e = float(s), float(e)
        if (groups and (s - groups[-1][1]) <= max_merge_gap
                and (groups[-1][1] - groups[-1][0]) < min_dur):
            groups[-1][1] = max(groups[-1][1], e)
        else:
            groups.append([s, e])
    return [(round(g[0], 2), round(g[1], 2)) for g in groups]


def g3_prescreen(
    silence_fraction: float | None,
    *,
    threshold: float = CLEAN_SILENCE_FRACTION,
) -> bool:
    """组2(c)：画像预筛——窗内几乎无静音气口 = 连续噪声/BGM 嫌疑 → 放行。

    阈值与 `audio_profile.CLEAN_SILENCE_FRACTION` 同源（连续噪声判据）。未知
    （None）时放行，交给 (a) 能量复核兜底——双门设计：预筛粗、能量复核准。
    """
    if silence_fraction is None:
        return True
    return silence_fraction < threshold


def g3_energy_verdict(
    vocals_db: float | None,
    other_db: float | None,
    *,
    other_min_db: float = -45.0,
    other_gap_db: float = 15.0,
) -> bool:
    """组2(a)：demucs 能量复核——other（伴奏）轨是否真有货。

    other 轨能量够高（>= other_min_db）且与 vocals 相差不大（<= other_gap_db）
    → demucs 真把 BGM 剥下来了 → 继续重解码。
    笑声/欢呼留 vocals、other 轨近乎静音 → 没东西可剥 → 跳过（**不白跑
    demucs**，这是笑声窗的止损点）。探测失败（None）一律保守返回 False。
    """
    if vocals_db is None or other_db is None:
        return False
    if other_db < other_min_db:
        return False
    return other_db >= vocals_db - other_gap_db


def select_g3_windows(
    candidates: list[dict[str, Any]],
    *,
    total_duration: float | None,
    budget_frac: float = 0.30,
    budget_abs: float = 600.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """组3：性能预算——总窗长 < 30% 视频时长 且 < 10min（两者取小）。

    按 ``score``（可疑度）降序贪心装入，超限的窗原样返回到 ``dropped`` 供报告。
    单个窗就超预算时不放行——验收要求预算是**硬上限**（G3 demucs 极贵）。
    """
    if not candidates:
        return [], []
    budget = (budget_abs if not total_duration
              else min(budget_frac * float(total_duration), budget_abs))
    ordered = sorted(candidates,
                     key=lambda c: (-float(c.get("score") or 0.0),
                                    float(c.get("start") or 0.0)))
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    used = 0.0
    for c in ordered:
        dur = float(c.get("end") or 0.0) - float(c.get("start") or 0.0)
        if used + dur <= budget:
            kept.append(c)
            used += dur
        else:
            dropped.append(c)
    kept.sort(key=lambda c: float(c.get("start") or 0.0))   # 确定性：按时间
    return kept, dropped


def g3_self_check_ok(
    segments: list[dict[str, Any]],
    silence_intervals: list[tuple[float, float]] | None,
    *,
    no_speech_thr: float = 0.6,
    logprob_thr: float = -1.0,
) -> bool:
    """组4：自校验退出——重解码结果再过一次双信号 review。

    仍被判 MISSING（有能量 + Whisper 自报可疑）→ False，标记「当前架构救不
    了」（典型：笑声与真音时间完全重叠，需未来的语音分类模型）。空结果也算
    没救回。
    """
    if not segments:
        return False
    verdicts = review_segments(
        segments, silence_intervals,
        no_speech_thr=no_speech_thr, logprob_thr=logprob_thr)
    return not any(v["verdict"] == MISSING for v in verdicts)


def fill_gaps(
    input_path: str,
    segments: list[dict[str, Any]],
    *,
    lang: str | None = None,
    min_gap: float = 2.0,
    model_name: str = "large-v3",
    threads: int | None = None,
    use_vad: bool = False,  # ADR-016 (T2a): accepted for CLI compat but IGNORED — recovery is always bare
    no_speech_threshold: float = NO_SPEECH_THRESHOLD,
    temperature: list[float] | None = None,
    collapse_min_dur: float = 4.0,
    collapse_ratio: float = 0.45,
    silence_intervals: list[tuple[float, float]] | None = None,
    silencedetect_noise: str = "-30dB",
    silencedetect_d: float = 0.3,
    device: str | None = None,
    compute_type: str | None = None,
    # T2 (ADR-017 / Spec 19 § (B)): force-decode recovery windows from the
    # SAME audio_source used in the main transcription pass (typically the
    # demucs-separated vocals.wav when the user ran --separate-vocals).
    # None → fall back to the historical behaviour (decode directly from
    # the original input_path video).
    # NOTE: silence_intervals / silencedetect STILL consult input_path
    # (Spec 19 Invariant #4) — the acoustic-fact reference is always the
    # original unmodified audio, never the cleaned source.
    audio_source: str | None = None,
    # ADR-034 §6.2 — post-transcribe dual-signal review + G1/G2 re-processing.
    review: bool = True,
    g1: bool = True,
    g2: bool = True,
    g1_min_sub: float = 1.0,
    g2_min_dur: float = 1.5,
    g2_max_windows: int = 20,
    # ADR-034 §6.3 — G3 局部 separate-vocals：只兜「强 BGM / 连续噪声掩盖真音」，
    # 不兜笑声（笑声是人声，demucs 分不开）。
    g3: bool = True,
    g3_min_dur: float = 5.0,
    g3_budget_frac: float = 0.30,
    g3_budget_abs: float = 600.0,
    g3_other_min_db: float = -45.0,
    g3_other_gap_db: float = 15.0,
    g3_demucs_model: str = "htdemucs",
    # G3 的 demucs 设备：默认 "auto" —— 在 Whisper 已占显存的前提下探测**剩余
    # 显存**，够（~1.5GB + 安全余量）就上 GPU，不够退 CPU（8GB 红线，ADR-034
    # §1.4）。可显式传 "cuda" / "cpu" 覆盖。
    g3_device: str = "auto",
    no_speech_thr: float = 0.6,
    logprob_thr: float = -1.0,
    min_energy_frac: float = 0.25,
    min_energy_abs: float = 0.25,
    # ADR-035 M3（Z2）: 合并视图段的信号 A 按 _raw_indices 回查这里的 raw 源段。
    raw_segments: list[dict[str, Any]] | None = None,
    # Independent cache layer (ADR-034 §5.2). When omitted the review still runs
    # but nothing is persisted, so a re-run re-processes the suspect windows.
    outdir: str | None = None,
    base: str | None = None,
    progress=print,
) -> list[dict[str, Any]]:
    """Audit `segments` for dropped speech in `input_path` and recover it.

    Two independent defects are probed:

      1. *time holes* — HEAD / interior / TAIL stretches with no segment at all;
      2. *in-segment collapse* — a long segment whose character density is far
         below the file median, i.e. whisper swallowed most of its speech.

    Returns a new, time-sorted segment list. Hole recoveries are inserted;
    collapsed segments are replaced by their re-decoded content when the decode
    yields materially more speech. If neither defect is present the input is
    returned unchanged (the audit is then essentially free).
    """
    if not segments:
        return segments

    # ADR-034 §5.2 — independent review cache. A re-run against an unchanged
    # timeline reuses the previous verdicts instead of re-decoding suspect
    # windows ("重跑只重处理可疑窗，不重跑全片").
    cache_path = review_cache_path(outdir, base) if (outdir and base) else None
    cache_digest = review_digest(segments) if cache_path else ""
    cache_params: dict[str, Any] = {}
    if cache_path:
        cache_params = {
            "review": review, "g1": g1, "g2": g2,
            "g1_min_sub": g1_min_sub, "g2_min_dur": g2_min_dur,
            "no_speech_thr": no_speech_thr, "logprob_thr": logprob_thr,
            "min_energy_frac": min_energy_frac, "min_energy_abs": min_energy_abs,
            "min_gap": min_gap, "collapse_min_dur": collapse_min_dur,
            "collapse_ratio": collapse_ratio, "lang": lang,
            "model": model_name,
            # G3 参数进缓存键：改 G3 配置即重算（与 review 同层，不进转写指纹）
            "g3": g3, "g3_min_dur": g3_min_dur,
            "g3_budget_frac": g3_budget_frac, "g3_budget_abs": g3_budget_abs,
            "g3_other_min_db": g3_other_min_db,
            "g3_other_gap_db": g3_other_gap_db,
            "g3_demucs_model": g3_demucs_model,
        }
        cached = _load_review_cache(cache_path, cache_digest, cache_params)
        if cached is not None:
            progress(f"[audit] review cache hit — reusing {len(cached)} "
                     f"segment(s), no re-decode "
                     f"({os.path.basename(cache_path)})")
            return cached

    threads = threads or os.cpu_count()
    total = probe_duration(input_path)

    # 1) collect holes — HEAD, interior, and TAIL
    holes: list[tuple[float, float]] = []
    if float(segments[0]["start"]) > min_gap:
        holes.append((0.0, float(segments[0]["start"])))
    for i in range(1, len(segments)):
        g = float(segments[i]["start"]) - float(segments[i - 1]["end"])
        if g > min_gap:
            holes.append((float(segments[i - 1]["end"]), float(segments[i]["start"])))
    if total and float(segments[-1]["end"]) < total - min_gap:
        holes.append((float(segments[-1]["end"]), float(total)))

    # Resolve the independent acoustic reference ONCE: the hole filter, the
    # dual-signal review, G1 slicing and G2 sub-window detection all read the
    # same silencedetect intervals (ADR-012 / Spec 19 Invariant #4). It is only
    # probed when something actually needs it, so a clean timeline stays free.
    review_on = review and (g1 or g2)
    if silence_intervals is not None:
        silences: list[tuple[float, float]] = list(silence_intervals)
        silences_ready = True
    elif holes or review_on:
        probed = _probe_silences_or_none(input_path, silencedetect_noise,
                                         silencedetect_d)
        silences = probed if probed is not None else []
        # B is the main judge, so it must be genuinely AVAILABLE. Note that an
        # empty-but-successful probe means "no silence anywhere" (every window
        # is energetic) — which must NOT be confused with "no reference", and is
        # precisely the case where G2 has the most to recover.
        silences_ready = probed is not None
    else:
        silences = []
        silences_ready = False

    # 1a) ADR-012: drop holes that are *genuine* silence (detected silence
    # interval fully covers the hole). Force-decoding these is what resurrects
    # isolated hallucinations into the timeline — leave them as silence.
    if holes and silences:
        kept: list[tuple[float, float]] = []
        for (gs, ge) in holes:
            if _hole_in_silence(gs, ge, silences):
                progress(f"[audit] hole {gs:.1f}->{ge:.1f}s: genuine silence "
                         f"(silencedetect) — skipped, not force-decoded")
            else:
                kept.append((gs, ge))
        holes = kept

    # 1b) collect in-segment collapses (continuous timeline, missing speech)
    collapsed = find_collapsed(
        segments, min_dur=collapse_min_dur, ratio=collapse_ratio
    )

    # 1c) ADR-034 §6.2 — dual-signal review + G2 detection. Both are pure, so
    # they run here only to decide whether the (expensive) Whisper model has to
    # be loaded at all. The authoritative detection re-runs after the
    # hole/collapse phase, on the merged timeline (step 4), so every index
    # refers to the list actually being mutated.
    g1_count = 0
    g2_count = 0
    if review_on and silences_ready:
        if g1:
            g1_count = len([
                s for s in review_segments(
                    segments, silences,
                    no_speech_thr=no_speech_thr, logprob_thr=logprob_thr,
                    min_energy_frac=min_energy_frac,
                    min_energy_abs=min_energy_abs, min_sub=g1_min_sub,
                    raw_segments=raw_segments,
                )
                if s["verdict"] == MISSING and len(s.get("g1_windows", [])) >= 2
            ])
        if g2:
            g2_count = sum(
                len(find_word_uncovered_subwindows(seg, silences,
                                                   min_dur=g2_min_dur))
                for seg in segments
            )

    if not holes and not collapsed and not g1_count and not g2_count:
        progress(f"[audit] no holes >= {min_gap}s, no collapsed segments, "
                 f"no suspect windows — coverage complete "
                 f"({len(segments)} segs)")
        if cache_path:
            _save_review_cache(cache_path, cache_digest, cache_params, segments)
        return segments

    progress(f"[audit] {len(holes)} hole(s) >= {min_gap}s + "
             f"{len(collapsed)} collapsed segment(s) + "
             f"{g1_count} G1 + {g2_count} G2 suspect window(s) to probe")

    # 2) force-decode each suspect window, drop echoes, splice real speech back
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    dev, ct = resolve_device(device, compute_type)
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device=dev, compute_type=ct,
                         cpu_threads=threads)

    def _decode_once(gs: float, ge: float, pad: float,
                     dedupe_pool: list[dict[str, Any]],
                     *,
                     check_overlap: bool = True) -> list[dict[str, Any]]:
        """Force-decode [gs-pad, ge+pad]; return non-echo, non-hallucination
        segments (absolute times).
        """
        ss = max(0.0, gs - pad)
        ee = min(total, ge + pad) if total else ge + pad
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav = tf.name
        _src: str = audio_source if audio_source else input_path
        try:
            extract_chunk(_src, wav, ss, ee - ss)
            segs, _ = model.transcribe(
                wav, language=lang, task="transcribe",
                beam_size=BEAM_SIZE, best_of=BEST_OF,
                condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
                repetition_penalty=REPETITION_PENALTY,
                # ADR-016 (T2a): recovery is ALWAYS bare. Forcing VAD here would
                # re-eject the very speech-under-noise this module exists to fix.
                vad_filter=False,
                no_speech_threshold=0.0,  # force-decode even near-silence
                temperature=temperature or TEMPERATURE_FALLBACK,
                word_timestamps=True,
            )
            out: list[dict[str, Any]] = []
            for s in segs:
                text = (s.text or "").strip()
                if not text:
                    continue
                if _is_echo(text, dedupe_pool):
                    continue  # leaked neighbour line, not new speech
                cand = {
                    "start": round(float(s.start) + ss, 2),
                    "end": round(float(s.end) + ss, 2),
                    "text": text,
                    "words": [
                        {"word": w.word, "start": round(float(w.start) + ss, 2),
                         "end": round(float(w.end) + ss, 2)}
                        for w in (s.words or [])
                    ],
                    "_recovered": True,
                }
                # ADR-020 addendum: carry Whisper confidence fields so the guard's
                # signal C works without re-decoding (mirrors transcribe._seg_to_dict).
                for fld in ("avg_logprob", "no_speech_prob", "compression_ratio"):
                    v = getattr(s, fld, None)
                    if v is not None:
                        cand[fld] = v
                # ADR-020 addendum: drop audio-sharing / impossible-rate / low-cfg
                # phantoms that slipped past the text-only _is_echo check.
                if _is_recovered_hallucination(cand, dedupe_pool,
                                               check_overlap=check_overlap):
                    continue
                out.append(cand)
            return out
        finally:
            if os.path.exists(wav):
                os.remove(wav)

    def _probe(gs: float, ge: float,
               dedupe_pool: list[dict[str, Any]],
               *,
               check_overlap: bool = True) -> list[dict[str, Any]]:
        """Decode a window robustly, working around whisper's prefix collapse.

        Whisper is acutely sensitive to what sits at the *start* of the decode
        window. If the pad reaches back far enough to catch the tail of the
        previous line, the decoder latches onto that fragment, emits it, and
        then predicts end-of-transcript for the entire remaining window. A real
        case: window 860.5->889.2s decoded with pad=0.5 yielded a single
        ``"than usual."`` (28s of speech lost); the very same window with
        pad=0.2 yielded 7 segments of genuine dialogue.

        So we do not bet on one pad. We probe with several, score each result by
        how much of the window it actually covers, and keep the best. The scan
        stops early once a probe covers most of the window, so the common case
        still costs a single decode.
        """
        window = max(ge - gs, 0.01)
        pads = _PROBE_PADS if window >= _MULTI_PROBE_MIN_WINDOW else _PROBE_PADS[:1]
        best: list[dict[str, Any]] = []
        best_cov = -1.0
        for pad in pads:
            cand = _decode_once(gs, ge, pad, dedupe_pool,
                                check_overlap=check_overlap)
            cov = sum(float(c["end"]) - float(c["start"]) for c in cand)
            if cov > best_cov:
                best, best_cov = cand, cov
            if best_cov >= _PROBE_GOOD_COVERAGE * window:
                break
        return best

    def _probe_long_hole(gs: float, ge: float,
                         dedupe_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Slice a very wide hole into sub-windows and force-decode each.

        ADR-016 (T2c): a single forced decode over a 30-50s hole is unreliable —
        whisper collapses it or hallucinates, and the pad rotation in ``_probe``
        cannot rescue it. We cut the hole into ``_SUBWIN``-second pieces (with
        ``_SUBWIN_OVERLAP`` overlap so a sentence straddling a cut is not clipped)
        and decode each independently with a small pad (neighbours can only leak
        a fraction of a second into a sub-window, so echo is naturally bounded
        there). Seam de-duplication happens once, globally, after all inserts are
        collected (see below).

        Returns merged, time-sorted, raw recovered segments for the hole.
        """
        subwins = _slice_long_hole(gs, ge)
        merged: list[dict[str, Any]] = []
        for (s0, s1) in subwins:
            merged.extend(_decode_once(s0, s1, _PROBE_PADS[0], dedupe_pool))
        return merged

    inserts: list[dict[str, Any]] = []

    for (gs, ge) in holes:
        if (ge - gs) > _SUBWIN:
            # ADR-016 (T2b): slice very wide holes for reliable recall
            recovered = _probe_long_hole(gs, ge, segments)
            tag = "long-hole"
        else:
            recovered = _probe(gs, ge, segments)
            tag = "hole"
        if recovered:
            inserts.extend(recovered)
            progress(f"[audit] {tag} {gs:.1f}->{ge:.1f}s: recovered "
                     f"{len(recovered)} seg(s): {recovered[0]['text'][:50]!r}")
        else:
            progress(f"[audit] {tag} {gs:.1f}->{ge:.1f}s: echo/empty — "
                     f"left as genuine silence")

    # 2a) global seam de-dup across all recovered inserts (ADR-016 T2c). Catches
    # duplicate fragments from long-hole sub-window overlap as well as any stray
    # neighbour-leak that slipped past _is_echo.
    inserts = _dedupe_seams(inserts)

    # 3) collapsed segments: re-decode the window; replace when we win content
    drop: set[int] = set()
    for idx in collapsed:
        seg = segments[idx]
        gs, ge = float(seg["start"]), float(seg["end"])
        pool = [s for j, s in enumerate(segments) if j != idx]
        # ADR-020 addendum: collapse replacement windows intentionally overlap
        # the replaced segment, so disable overlap signal A here (the replacement
        # speech must not be mis-dropped); B (rate) and C (confidence) still apply.
        recovered = _probe(gs, ge, pool, check_overlap=False)
        orig_len = len((seg.get("text") or "").strip())
        new_len = sum(len(r["text"]) for r in recovered)
        if recovered and (len(recovered) >= 2 or new_len > orig_len * 1.6):
            drop.add(idx)
            inserts.extend(recovered)
            progress(f"[audit] collapse {gs:.1f}->{ge:.1f}s (cps={_cps(seg):.1f}): "
                     f"replaced with {len(recovered)} seg(s), "
                     f"{orig_len} -> {new_len} chars")
        else:
            progress(f"[audit] collapse {gs:.1f}->{ge:.1f}s: no extra speech — "
                     f"kept original")

    # ------------------------------------------------------------------ #
    # ADR-034 §6.2 — G1 / G2 re-processing.
    # Nested so they close over the already-loaded Whisper model, the probe
    # helpers and the single silencedetect reference.
    # ------------------------------------------------------------------ #
    def _merge_words_into(seg: dict[str, Any],
                          recovered: list[dict[str, Any]]) -> int:
        """Merge recovered WORDS back into `seg` (in place). Returns count added.

        G2 splices words, not whole cues: the recovered window sits INSIDE the
        parent's own span, so adding it as a sibling cue would produce an
        overlapping timeline and a duplicated subtitle. Merging the word stream
        keeps the timeline monotonic while restoring the lost speech — exactly
        ADR-034 §3.3's "拿补回 words".
        """
        if not seg.get("words"):
            return 0
        words = list(seg["words"])
        added = 0
        for r in recovered:
            for w in (r.get("words") or []):
                words.append(w)
                added += 1
        if not added:
            return 0
        words.sort(key=lambda w: (float(w.get("start") or 0.0),
                                  float(w.get("end") or 0.0)))
        for w in words:
            w["start"] = round(float(w.get("start") or 0.0), 2)
            w["end"] = round(float(w.get("end") or 0.0), 2)
        seg["words"] = words
        # Rebuild text from the (now complete) word stream: faster-whisper words
        # carry their own leading whitespace, so a plain concat stays faithful to
        # the model's own tokenisation.
        rebuilt = "".join(str(w.get("word") or "") for w in words).strip()
        if rebuilt:
            seg["text"] = rebuilt
        cur_s = float(seg.get("start") or words[0]["start"])
        cur_e = float(seg.get("end") or words[-1]["end"])
        seg["start"] = round(min(cur_s, float(words[0]["start"])), 2)
        seg["end"] = round(max(cur_e, float(words[-1]["end"])), 2)
        # ADR-031 D2 spirit: make the amendment visible to verify / reread.
        seg["_g2_recovered"] = True
        return added

    def _apply_g1() -> int:
        """G1 — re-decode the silence-sliced sub-windows of suspect segments.

        ADR-034 §3.2: speech glued to laughter/noise only decodes correctly once
        the silence gap between them is used as a cut point. Requires >= 2
        slices — with no silence inside, G1 cannot cut and the window falls
        through to G2/G3.
        """
        fixes = 0
        suspects = [
            s for s in review_segments(
                merged, silences,
                no_speech_thr=no_speech_thr, logprob_thr=logprob_thr,
                min_energy_frac=min_energy_frac, min_energy_abs=min_energy_abs,
                min_sub=g1_min_sub, raw_segments=raw_segments,
            )
            if s["verdict"] == MISSING and len(s.get("g1_windows", [])) >= 2
        ]
        if not suspects:
            return 0
        drop_idx: set[int] = set()
        news: list[dict[str, Any]] = []
        for rec in suspects:
            idx = rec["index"]
            if idx in drop_idx:
                continue
            pool = [s for j, s in enumerate(merged) if j != idx]
            recovered: list[dict[str, Any]] = []
            for (a, b) in rec["g1_windows"]:
                # check_overlap=False: every slice sits INSIDE the suspect's own
                # span, so the guard's overlap signal (A) would always fire.
                # B (rate), C (confidence) and D (zero-duration words) still apply.
                recovered.extend(
                    _decode_once(a, b, _PROBE_PADS[0], pool,
                                 check_overlap=False)
                )
            if not recovered:
                continue
            recovered = _dedupe_seams(recovered)
            orig_len = len(rec["text"])
            new_len = sum(len(r["text"]) for r in recovered)
            if len(recovered) >= 2 or new_len > orig_len * 1.6:
                drop_idx.add(idx)
                news.extend(recovered)
                fixes += 1
                progress(f"[audit] G1 {rec['start']:.1f}->{rec['end']:.1f}s: "
                         f"{len(rec['g1_windows'])} slice(s) -> "
                         f"{len(recovered)} seg(s), "
                         f"{orig_len} -> {new_len} chars")
        if not news:
            return 0
        for i in sorted(drop_idx, reverse=True):
            del merged[i]
        merged.extend(news)
        return fixes

    def _apply_g2() -> int:
        """G2 — recover speech inside segments whose words leave energy gaps.

        ADR-034 §3.3: the timeline is continuous, so a segment-gap scan sees
        nothing wrong even when most of a segment's speech was collapsed away.
        We re-decode the energetic, word-uncovered sub-window and merge the
        recovered words back into the parent.
        """
        targets: list[tuple[int, tuple[float, float]]] = []
        for i, seg in enumerate(merged):
            for w in find_word_uncovered_subwindows(seg, silences,
                                                    min_dur=g2_min_dur):
                targets.append((i, w))
        if not targets:
            return 0
        if len(targets) > g2_max_windows:
            progress(f"[audit] G2: {len(targets)} suspect sub-window(s) found; "
                     f"budget {g2_max_windows} — keeping the widest")
            targets.sort(key=lambda t: (t[1][1] - t[1][0]), reverse=True)
            targets = targets[:g2_max_windows]
        targets.sort()  # deterministic: by segment index, then window start
        fixes = 0
        for (idx, (a, b)) in targets:
            seg = merged[idx]
            pool = [s for j, s in enumerate(merged) if j != idx]
            # check_overlap=False: the sub-window lies inside the parent's own
            # span by construction; B/C/D guard signals still apply.
            recovered = _decode_once(a, b, _PROBE_PADS[0], pool,
                                     check_overlap=False)
            if not recovered:
                continue
            added = _merge_words_into(seg, recovered)
            if not added:
                continue
            fixes += 1
            progress(f"[audit] G2 seg#{idx} {a:.1f}->{b:.1f}s: "
                     f"+{added} word(s) merged back")
        return fixes

    def _decode_from_file(wav: str, base_start: float,
                          dedupe_pool: list[dict[str, Any]],
                          ) -> list[dict[str, Any]]:
        """解码一段**本身就是窗口长度**的 wav（G3 的 demucs 人声轨）。

        与 ``_decode_once`` 的区别：源音频已经是窗口切片，不需要再裁剪，只需
        把解码结果的时间戳整体平移回 ``base_start``（绝对时间轴）。
        """
        try:
            segs, _ = model.transcribe(
                wav, language=lang, task="transcribe",
                beam_size=BEAM_SIZE, best_of=BEST_OF,
                condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
                repetition_penalty=REPETITION_PENALTY,
                # 同 fill_gaps 一贯立场：恢复解码恒为裸跑
                vad_filter=False,
                no_speech_threshold=0.0,
                temperature=temperature or TEMPERATURE_FALLBACK,
                word_timestamps=True,
            )
        except Exception:  # noqa: BLE001 - 解码失败 = 这个窗没救回
            return []
        out: list[dict[str, Any]] = []
        for s in segs:
            text = (s.text or "").strip()
            if not text or _is_echo(text, dedupe_pool):
                continue
            cand = {
                "start": round(float(s.start) + base_start, 2),
                "end": round(float(s.end) + base_start, 2),
                "text": text,
                "words": [
                    {"word": w.word,
                     "start": round(float(w.start) + base_start, 2),
                     "end": round(float(w.end) + base_start, 2)}
                    for w in (s.words or [])
                ],
                "_recovered": True,
            }
            for fld in ("avg_logprob", "no_speech_prob", "compression_ratio"):
                v = getattr(s, fld, None)
                if v is not None:
                    cand[fld] = v
            # check_overlap=False: 窗口本就落在可疑段自己的跨度内
            if _is_recovered_hallucination(cand, dedupe_pool,
                                           check_overlap=False):
                continue
            out.append(cand)
        return out

    def _apply_g3() -> tuple[int, list[dict[str, Any]]]:
        """G3 — 对 G1/G2 都救不回的可疑窗做局部 demucs 分离后重解码。

        ADR-034 §3.4 四组门控，缺一不可：
          组1 硬前置：review 判 MISSING ∧ 该段未被 G1/G2 救回 ∧ 窗长 ≥5s（短窗合并）
          组2 双门：(c) 画像预筛（连续噪声）→ (a) demucs 能量复核（other 轨有货）
          组3 预算：总窗长 < 30% 视频 且 < 10min
          组4 自校验：重解码后再过 review，仍 MISSING → 标记救不了、**不缝合**
        """
        unrecoverable: list[dict[str, Any]] = []
        # 组1：G1/G2 跑完仍 MISSING、且未被救回过的段（即 G1 空 ∧ G2 空）
        remaining = [
            r for r in review_segments(
                merged, silences,
                no_speech_thr=no_speech_thr, logprob_thr=logprob_thr,
                min_energy_frac=min_energy_frac, min_energy_abs=min_energy_abs,
                min_sub=g1_min_sub, raw_segments=raw_segments,
            )
            if r["verdict"] == MISSING
            and not merged[r["index"]].get("_g2_recovered")
            and not merged[r["index"]].get("_recovered")
        ]
        if not remaining:
            return 0, unrecoverable

        windows = group_short_windows(
            [(float(r["start"]), float(r["end"])) for r in remaining],
            min_dur=g3_min_dur)

        # 组2(c) 画像预筛：**窗级** silencedetect 探测连续噪声/BGM（贴合
        # ADR §3.4「该窗所在 chunk 画像」）。候选窗受预算封顶（≤10min），每窗
        # 一次短窗 ffmpeg 探测（亚秒级）；探测失败返回 None → 预筛放行，交给
        # (a) 能量复核兜底（双门）。
        candidates: list[dict[str, Any]] = []
        for (a, b) in windows:
            sf = probe_window_silence_fraction(
                input_path, a, b,
                noise=silencedetect_noise, d=silencedetect_d)
            if not g3_prescreen(sf):
                shown = f"{sf:.2f}" if sf is not None else "unknown"
                progress(f"[audit] G3 {a:.1f}->{b:.1f}s: pre-screen blocked "
                         f"(silence fraction {shown}) — not a BGM/continuous "
                         f"masker, skipping demucs")
                continue
            candidates.append({"start": a, "end": b, "score": b - a})
        if not candidates:
            return 0, unrecoverable

        # 组3 性能预算
        kept, dropped = select_g3_windows(
            candidates, total_duration=total,
            budget_frac=g3_budget_frac, budget_abs=g3_budget_abs)
        if dropped:
            progress(f"[audit] G3: {len(dropped)} window(s) over budget "
                     f"({g3_budget_frac:.0%} of video, cap {g3_budget_abs:.0f}s)"
                     f" — only the most suspicious {len(kept)} processed")

        from .audio_profile import probe_volume_window
        fixes = 0
        for cand in kept:
            a, b = float(cand["start"]), float(cand["end"])
            idxs = [r["index"] for r in remaining
                    if float(r["start"]) < b and float(r["end"]) > a]
            pool = [s for j, s in enumerate(merged) if j not in idxs]
            # 局部分离（按窗缓存；demucs 不可用时返回 (None, None)）
            try:
                from .vocal_sep import pick_separation_device, separate_window
                vocals_wav, other_wav = separate_window(
                    input_path, a, b, outdir or os.path.dirname(input_path) or ".",
                    base=base or "", model_name=g3_demucs_model,
                    device=pick_separation_device(g3_device, progress=progress),
                    progress=progress)
            except Exception as exc:  # noqa: BLE001
                progress(f"[audit] G3 {a:.1f}->{b:.1f}s: separation failed "
                         f"({exc}) — skipped")
                continue
            if not vocals_wav:
                progress(f"[audit] G3 {a:.1f}->{b:.1f}s: demucs unavailable — "
                         f"skipped (install demucs or pass --no-g3)")
                continue

            # 组2(a) 能量复核：other 轨有货才说明真剥下了 BGM
            try:
                v_mean, _ = probe_volume_window(vocals_wav, 0.0, b - a)
                o_mean, _ = (probe_volume_window(other_wav, 0.0, b - a)
                             if other_wav else (None, None))
            except Exception:  # noqa: BLE001
                v_mean = o_mean = None
            if not g3_energy_verdict(v_mean, o_mean,
                                     other_min_db=g3_other_min_db,
                                     other_gap_db=g3_other_gap_db):
                progress(f"[audit] G3 {a:.1f}->{b:.1f}s: energy recheck says "
                         f"nothing separated (vocals={v_mean}, other={o_mean}) "
                         f"— laughter-like window, skipped (no wasted decode)")
                continue

            recovered = _decode_from_file(vocals_wav, a, pool)
            # 组4 自校验：仍 MISSING = 当前架构救不了（完全重叠的笑声+真音）
            if not g3_self_check_ok(recovered, silences,
                                    no_speech_thr=no_speech_thr,
                                    logprob_thr=logprob_thr):
                unrecoverable.append({
                    "start": round(a, 2), "end": round(b, 2),
                    "reason": "self-check still MISSING (likely fully "
                              "overlapping laughter+speech)"})
                progress(f"[audit] G3 {a:.1f}->{b:.1f}s: self-check still "
                         f"MISSING — marked unrecoverable, NOT spliced")
                continue
            if not recovered:
                continue
            orig_len = sum(len((merged[i].get("text") or "").strip())
                           for i in idxs)
            new_len = sum(len(r["text"]) for r in recovered)
            if len(recovered) >= 2 or new_len > orig_len * 1.6:
                for i in sorted(idxs, reverse=True):
                    del merged[i]
                for r in recovered:
                    r["_g3_recovered"] = True
                merged.extend(recovered)
                fixes += 1
                progress(f"[audit] G3 {a:.1f}->{b:.1f}s: demucs separation "
                         f"recovered {len(recovered)} seg(s), "
                         f"{orig_len} -> {new_len} chars")
        return fixes, unrecoverable

    merged = [s for i, s in enumerate(segments) if i not in drop] + inserts
    merged.sort(key=lambda x: float(x["start"]))

    # 4) G1 then G2 then G3, on the merged timeline (indices are valid HERE).
    g1_fixes = g2_fixes = g3_fixes = 0
    g3_unrecoverable: list[dict[str, Any]] = []
    if review_on and silences_ready:
        if g1:
            g1_fixes = _apply_g1()
        if g2:
            g2_fixes = _apply_g2()
        if g1_fixes or g2_fixes:
            merged.sort(key=lambda x: float(x["start"]))
        if g3:
            g3_fixes, g3_unrecoverable = _apply_g3()
            if g3_fixes:
                merged.sort(key=lambda x: float(x["start"]))

    progress(f"[audit] +{len(inserts)} recovered / -{len(drop)} collapsed "
             f"+{g1_fixes} G1 +{g2_fixes} G2 +{g3_fixes} G3 "
             f"-> {len(merged)} total")
    if g3_unrecoverable:
        shown = ", ".join(f"{w['start']:.1f}-{w['end']:.1f}s"
                          for w in g3_unrecoverable[:5])
        progress(f"[audit] G3 unrecoverable {len(g3_unrecoverable)} window(s) "
                 f"(current architecture cannot separate): {shown}")
    if cache_path:
        _save_review_cache(cache_path, cache_digest, cache_params, merged,
                           g3_unrecoverable=g3_unrecoverable)
    return merged