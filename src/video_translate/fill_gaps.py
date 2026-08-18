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
import os
import re
import statistics
import tempfile
from typing import Any

from .ffmpeg_utils import extract_chunk, probe_duration
from .transcribe import (
    DEVICE, COMPUTE_TYPE, BEAM_SIZE, BEST_OF,
    CONDITION_ON_PREVIOUS_TEXT, REPETITION_PENALTY,
    NO_SPEECH_THRESHOLD, TEMPERATURE_FALLBACK, build_vad_params,
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


def _norm_tokens(s: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


def _jaccard(a: str, b: str) -> float:
    A, B = _norm_tokens(a), _norm_tokens(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


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


def _cps(seg: dict[str, Any]) -> float:
    dur = float(seg["end"]) - float(seg["start"])
    return len((seg.get("text") or "").strip()) / max(dur, 0.01)


_EPS = 1e-3


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


def fill_gaps(
    input_path: str,
    segments: list[dict[str, Any]],
    *,
    lang: str | None = None,
    min_gap: float = 2.0,
    model_name: str = "large-v3",
    threads: int | None = None,
    use_vad: bool = False,
    no_speech_threshold: float = NO_SPEECH_THRESHOLD,
    temperature: list[float] | None = None,
    collapse_min_dur: float = 4.0,
    collapse_ratio: float = 0.45,
    silence_intervals: list[tuple[float, float]] | None = None,
    silencedetect_noise: str = "-30dB",
    silencedetect_d: float = 0.3,
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

    # 1a) ADR-012: drop holes that are *genuine* silence (detected silence
    # interval fully covers the hole). Force-decoding these is what resurrects
    # isolated hallucinations into the timeline — leave them as silence.
    if holes:
        silences = (
            silence_intervals
            if silence_intervals is not None
            else _profile_silences(input_path, silencedetect_noise, silencedetect_d)
        )
        if silences:
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

    if not holes and not collapsed:
        progress(f"[audit] no holes >= {min_gap}s, no collapsed segments — "
                 f"coverage complete ({len(segments)} segs)")
        return segments

    progress(f"[audit] {len(holes)} hole(s) >= {min_gap}s + "
             f"{len(collapsed)} collapsed segment(s) to probe")

    # 2) force-decode each suspect window, drop echoes, splice real speech back
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    vad_params = build_vad_params(None)
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device=DEVICE, compute_type=COMPUTE_TYPE,
                         cpu_threads=threads)

    def _decode_once(gs: float, ge: float, pad: float,
                     dedupe_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Force-decode [gs-pad, ge+pad]; return non-echo segments (absolute times)."""
        ss = max(0.0, gs - pad)
        ee = min(total, ge + pad) if total else ge + pad
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav = tf.name
        try:
            extract_chunk(input_path, wav, ss, ee - ss)
            segs, _ = model.transcribe(
                wav, language=lang, task="transcribe",
                beam_size=BEAM_SIZE, best_of=BEST_OF,
                condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
                repetition_penalty=REPETITION_PENALTY,
                vad_filter=use_vad,
                **(dict(vad_parameters=vad_params) if use_vad else {}),
                no_speech_threshold=0.0,  # force-decode the silence
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
                out.append({
                    "start": round(s.start + ss, 2),
                    "end": round(s.end + ss, 2),
                    "text": text,
                    "words": [
                        {"word": w.word, "start": round(w.start + ss, 2),
                         "end": round(w.end + ss, 2)}
                        for w in (s.words or [])
                    ],
                    "_recovered": True,
                })
            return out
        finally:
            if os.path.exists(wav):
                os.remove(wav)

    def _probe(gs: float, ge: float,
               dedupe_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            cand = _decode_once(gs, ge, pad, dedupe_pool)
            cov = sum(float(c["end"]) - float(c["start"]) for c in cand)
            if cov > best_cov:
                best, best_cov = cand, cov
            if best_cov >= _PROBE_GOOD_COVERAGE * window:
                break
        return best

    inserts: list[dict[str, Any]] = []

    for (gs, ge) in holes:
        recovered = _probe(gs, ge, segments)
        if recovered:
            inserts.extend(recovered)
            progress(f"[audit] hole {gs:.1f}->{ge:.1f}s: recovered "
                     f"{len(recovered)} seg(s): {recovered[0]['text'][:50]!r}")
        else:
            progress(f"[audit] hole {gs:.1f}->{ge:.1f}s: echo/empty — "
                     f"left as genuine silence")

    # 3) collapsed segments: re-decode the window; replace when we win content
    drop: set[int] = set()
    for idx in collapsed:
        seg = segments[idx]
        gs, ge = float(seg["start"]), float(seg["end"])
        pool = [s for j, s in enumerate(segments) if j != idx]
        recovered = _probe(gs, ge, pool)
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

    merged = [s for i, s in enumerate(segments) if i not in drop] + inserts
    merged.sort(key=lambda x: float(x["start"]))
    progress(f"[audit] +{len(inserts)} recovered / -{len(drop)} collapsed -> "
             f"{len(merged)} total")
    return merged
