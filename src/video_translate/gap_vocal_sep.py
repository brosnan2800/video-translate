"""Hard-gap vocal separation recovery (ADR-030 / Spec 24).

Optional enhancement to ``fill_gaps``: for large, high-energy holes that were
missed by the main transcription pass, run Demucs vocal separation on just that
window, then decode the cleaned vocals. A stricter hallucination guard is applied
to the results to avoid inserting BGM lyrics or laughter hallucinations.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from typing import Any, Callable

from .audio_profile import parse_volumedetect
from .ffmpeg_utils import _resolve_binary, extract_chunk, probe_duration
from .fill_gaps import _is_recovered_hallucination
from .io_utils import flush_print
from .vocal_sep import separate_vocals, resolve_vsep_route


# ---------------------------------------------------------------------------
# Window energy analysis
# ---------------------------------------------------------------------------

def _window_energy(input_path: str, start: float, end: float) -> tuple[float | None, float | None]:
    """Return (mean_db, max_db) for the window [start, end) via ffmpeg volumedetect.

    Returns (None, None) on failure so callers can treat it as "no signal".
    """
    dur = max(0.1, end - start)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-ss", str(start), "-t", str(dur),
        "-i", input_path,
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    cmd[0] = _resolve_binary("ffmpeg")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        return None, None
    if proc.returncode != 0:
        return None, None
    return parse_volumedetect(proc.stderr)


# ---------------------------------------------------------------------------
# Hard-gap selection
# ---------------------------------------------------------------------------

def select_hard_gaps(
    input_path: str,
    holes: list[tuple[float, float]],
    *,
    min_gap: float = 5.0,
    energy_mean_db: float = -30.0,
    energy_max_db: float = -10.0,
    progress: Callable[..., None] = flush_print,
) -> list[tuple[float, float]]:
    """Filter holes down to high-energy hard gaps worth running Demucs on.

    A hole is selected when:
      * its duration >= min_gap, and
      * its mean volume > energy_mean_db OR max volume > energy_max_db.
    """
    hard: list[tuple[float, float]] = []
    for (gs, ge) in holes:
        if ge - gs < min_gap:
            continue
        mean_db, max_db = _window_energy(input_path, gs, ge)
        if mean_db is None and max_db is None:
            progress(f"[gap-vocal-sep] hole {gs:.1f}->{ge:.1f}s: energy probe failed — skipped")
            continue
        loud = (
            (mean_db is not None and mean_db > energy_mean_db)
            or (max_db is not None and max_db > energy_max_db)
        )
        if not loud:
            progress(f"[gap-vocal-sep] hole {gs:.1f}->{ge:.1f}s: quiet "
                     f"(mean={mean_db}, max={max_db} dB) — skipped")
            continue
        progress(f"[gap-vocal-sep] hole {gs:.1f}->{ge:.1f}s: hard gap "
                 f"(mean={mean_db}, max={max_db} dB)")
        hard.append((gs, ge))
    return hard


# ---------------------------------------------------------------------------
# Strict hallucination guard for gap-vocal-sep recovered segments
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _looks_like_lyrics(text: str) -> bool:
    """Heuristic: detect repetitive / chant-like text typical of BGM lyrics.

    Detects a short token sequence that repeats to make up most of the text,
    e.g. "Inside you so deep inside you so deep" or "yeah yeah yeah".
    """
    t = (text or "").lower().strip()
    if not t:
        return False
    tokens = _TOKEN_RE.findall(t)
    n = len(tokens)
    if n < 4:
        return False

    # Find the smallest repeating unit. If the token list is composed of a unit
    # repeated 2+ times, treat it as lyrics/chant.
    for unit_len in range(1, n // 2 + 1):
        if n % unit_len != 0:
            continue
        unit = tokens[:unit_len]
        if all(tokens[i:i + unit_len] == unit for i in range(0, n, unit_len)):
            return True

    return False


def _is_gap_vocal_hallucination(
    cand: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    no_speech_thr: float = 0.5,
    avg_logprob_thr: float = -0.8,
    overlap_eps: float = 0.12,
    max_wps: float = 8.0,
    min_zero_dur_words: int = 2,
) -> bool:
    """Stricter hallucination guard for gap-vocal-sep recovered segments.

    Tightens ADR-021's _is_recovered_hallucination:
      * no_speech_prob threshold lower (0.5 vs 0.6)
      * avg_logprob threshold higher (-0.8 vs -1.0)
      * adds lyrics/repetition pattern detection
    """
    # Reuse the geometric/physics signals from ADR-021.
    if _is_recovered_hallucination(
        cand, segments,
        overlap_eps=overlap_eps,
        max_wps=max_wps,
        min_zero_dur_words=min_zero_dur_words,
        avg_logprob_thr=avg_logprob_thr,
        no_speech_thr=no_speech_thr,
    ):
        return True

    # Lyrics / chant pattern ( Spec 24 signal F).
    if _looks_like_lyrics(cand.get("text", "")):
        return True

    return False


# ---------------------------------------------------------------------------
# Decode hard gaps from the cleaned vocals map
# ---------------------------------------------------------------------------

def _seg_to_dict(s, offset: float) -> dict[str, Any]:
    """Mirror of transcribe._seg_to_dict for gap-vocal-sep decode path."""
    seg: dict[str, Any] = {
        "start": round(float(s.start) + offset, 2),
        "end": round(float(s.end) + offset, 2),
        "text": (s.text or "").strip(),
        "words": [
            {"word": w.word, "start": round(float(w.start) + offset, 2),
             "end": round(float(w.end) + offset, 2)}
            for w in (s.words or [])
        ],
        "_gap_vocal_recovered": True,
    }
    for fld in ("avg_logprob", "no_speech_prob", "compression_ratio"):
        v = getattr(s, fld, None)
        if v is not None:
            seg[fld] = v
    return seg


def _decode_gap_vocals(
    vocals_map: dict[tuple[float, float], tuple[str, float]],
    *,
    model: Any,
    lang: str | None,
    segments: list[dict[str, Any]],
    no_speech_thr: float = 0.5,
    avg_logprob_thr: float = -0.8,
    progress: Callable[..., None] = flush_print,
) -> list[dict[str, Any]]:
    """Decode each hard gap from its cleaned vocals.wav and apply strict filtering.

    ``vocals_map`` maps a gap -> ``(vocals_path, local_start)`` where
    ``local_start`` is the offset of that gap INSIDE ``vocals_path``:

      * window-level Demucs produces a file that only holds the gap, so
        ``local_start`` is 0.0;
      * reusing the global vocals.wav (ADR-030 §5) hands back the FULL-length
        track, so ``local_start`` is the gap's own ``start``.

    Only the gap window is ever decoded. Decoding the whole global track here
    would both cost a full-video pass per gap and timestamp every recovered
    line against the wrong part of the timeline.

    ``model`` is an already-loaded faster-whisper WhisperModel; the caller
    (fill_gaps) owns its lifecycle.
    """
    from .fill_gaps import _is_echo

    recovered: list[dict[str, Any]] = []
    for (gs, ge), (vocals_path, local_start) in sorted(vocals_map.items()):
        if not os.path.isfile(vocals_path):
            progress(f"[gap-vocal-sep] gap {gs:.1f}->{ge:.1f}s: vocals file missing — skipped")
            continue
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav = tf.name
        try:
            # Copy just this gap's window so extract_chunk can overwrite in place.
            extract_chunk(vocals_path, wav, local_start, ge - gs)
            segs, _ = model.transcribe(
                wav, language=lang, task="transcribe",
                # Use the same production recipe as fill_gaps.
                beam_size=5, best_of=5,
                condition_on_previous_text=False,
                repetition_penalty=1.1,
                vad_filter=False,
                no_speech_threshold=0.0,
                temperature=[0.0, 0.2, 0.4],
                word_timestamps=True,
            )
            for s in segs:
                text = (s.text or "").strip()
                if not text:
                    continue
                if _is_echo(text, segments + recovered):
                    continue
                cand = _seg_to_dict(s, gs)
                if _is_gap_vocal_hallucination(
                    cand, segments + recovered,
                    no_speech_thr=no_speech_thr,
                    avg_logprob_thr=avg_logprob_thr,
                ):
                    progress(f"[gap-vocal-sep] filtered: {text[:50]!r}")
                    continue
                recovered.append(cand)
                progress(f"[gap-vocal-sep] recovered {gs:.1f}->{ge:.1f}s: {text[:50]!r}")
        finally:
            if os.path.exists(wav):
                os.remove(wav)
    return recovered


# ---------------------------------------------------------------------------
# Main entry: select hard gaps and produce cleaned vocals for each
# ---------------------------------------------------------------------------

def recover_hard_gaps(
    input_path: str,
    segments: list[dict[str, Any]],
    holes: list[tuple[float, float]],
    *,
    min_gap: float = 5.0,
    energy_mean_db: float = -30.0,
    energy_max_db: float = -10.0,
    audio_source: str | None = None,
    progress: Callable[..., None] = flush_print,
) -> dict[tuple[float, float], tuple[str, float]]:
    """Return a mapping from hard gap to ``(vocals_path, local_start)``.

    If ``audio_source`` (global vocals.wav from --separate-vocals) is provided,
    it is reused directly and no window-level Demucs is run. Otherwise each hard
    gap is extracted to a temporary file, run through Demucs, and the resulting
    vocals path is recorded.

    ``local_start`` is where this gap begins inside the returned file: 0.0 for a
    window-level separation (the file holds only the gap) and the gap's own
    start when the full-length global track is reused.
    """
    hard_gaps = select_hard_gaps(
        input_path, holes,
        min_gap=min_gap,
        energy_mean_db=energy_mean_db,
        energy_max_db=energy_max_db,
        progress=progress,
    )
    if not hard_gaps:
        return {}

    vocals_map: dict[tuple[float, float], tuple[str, float]] = {}

    # Fast path: global vocals.wav already exists.
    if audio_source and os.path.isfile(audio_source):
        progress(f"[gap-vocal-sep] reusing global vocals.wav for {len(hard_gaps)} hard gap(s)")
        for (gs, ge) in hard_gaps:
            # Duration invariant: the global vocals.wav must match the full input;
            # we only assert the requested window fits inside it.
            try:
                dur = probe_duration(audio_source)
                if ge <= dur + 0.05:
                    # The global track is full-length, so this gap starts at its
                    # own absolute offset inside the file.
                    vocals_map[(gs, ge)] = (audio_source, gs)
                else:
                    progress(f"[gap-vocal-sep] gap {gs:.1f}->{ge:.1f}s: "
                             f"outside global vocals duration {dur:.1f}s — will separate locally")
            except Exception as exc:  # noqa: BLE001
                progress(f"[gap-vocal-sep] probe global vocals failed ({exc}); "
                         f"falling back to local separation")
        # Any gaps that couldn't use the global track fall through to local Demucs.
        hard_gaps = [g for g in hard_gaps if g not in vocals_map]

    if not hard_gaps:
        return vocals_map

    # Hard gate (Spec 25 / ADR-031): any gap that reaches here needs a LOCAL
    # Demucs run, which only a ready CUDA lane may host. Every other lane
    # refuses the whole local-separation pass with ONE clear warning instead of
    # grinding on the CPU gap by gap. Gaps already covered by a cached global
    # vocals.wav (audio_source) are kept — they needed no Demucs at all.
    route = resolve_vsep_route()
    if not route.can_separate:
        progress(f"[gap-vocal-sep] recovering hard gaps needs Demucs, but this "
                 f"machine cannot host it: {route.message} "
                 f"Skipping local separation — these gaps fall back to the "
                 f"original audio.")
        return vocals_map

    total = len(hard_gaps)
    progress(f"[gap-vocal-sep] running Demucs on {total} hard gap(s) ...")
    with tempfile.TemporaryDirectory(prefix="gap_vsep_") as tmpdir:
        for idx, (gs, ge) in enumerate(hard_gaps, 1):
            window_dur = ge - gs
            window_wav = os.path.join(tmpdir, f"gap_{gs:.2f}_{ge:.2f}.wav")
            # Per-gap timing: separation is the only stage that can cost minutes
            # per window, so each gap must report its own cost as it happens.
            started = time.monotonic()
            progress(f"[gap-vocal-sep] ({idx}/{total}) gap {gs:.1f}->{ge:.1f}s "
                     f"({window_dur:.1f}s window): separating ...")
            try:
                extract_chunk(input_path, window_wav, gs, window_dur)
            except Exception as exc:  # noqa: BLE001
                progress(f"[gap-vocal-sep] gap {gs:.1f}->{ge:.1f}s: extract failed ({exc}) — skipped")
                continue

            gap_vocals = separate_vocals(
                window_wav, tmpdir, base=f"gap_{gs:.2f}_{ge:.2f}",
                progress=progress,
            )
            if gap_vocals is None:
                progress(f"[gap-vocal-sep] gap {gs:.1f}->{ge:.1f}s: Demucs failed "
                         f"({time.monotonic() - started:.1f}s) — skipped")
                continue

            try:
                out_dur = probe_duration(gap_vocals)
            except Exception as exc:  # noqa: BLE001
                progress(f"[gap-vocal-sep] gap {gs:.1f}->{ge:.1f}s: probe failed ({exc}) — skipped")
                continue

            if abs(out_dur - window_dur) >= 0.05:
                progress(f"[gap-vocal-sep] gap {gs:.1f}->{ge:.1f}s: duration mismatch "
                         f"(window={window_dur:.3f}s vocals={out_dur:.3f}s) — skipped")
                continue

            # A window-level separation holds only this gap: local start is 0.0.
            vocals_map[(gs, ge)] = (gap_vocals, 0.0)
            progress(f"[gap-vocal-sep] ({idx}/{total}) gap {gs:.1f}->{ge:.1f}s: "
                     f"vocals ready in {time.monotonic() - started:.1f}s "
                     f"({os.path.basename(gap_vocals)})")

    return vocals_map
