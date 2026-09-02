"""Audio profile — the independent acoustic reference (ADR-012 / Spec 18).

Whisper's word/segment timestamps are DTW *posterior estimates*: they drift and
collapse, they are NOT ground truth. This module produces an independent
reference from `ffmpeg` filters (volumedetect level + silencedetect gaps) so the
pipeline can (a) route VAD automatically in `doctor` and (b) cross-check cue
alignment in `verify` against measured silence — instead of trusting whisper's
self-asserted timestamps.

All parsing functions are pure (no subprocess) so they are unit-testable with
synthetic ffmpeg stderr. `analyze_audio` is the only function that shells out.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from .ffmpeg_utils import (
    _resolve_binary,
    build_audio_profile_cmd,
    extract_chunk,
    probe_duration,
)

# VAD routing thresholds (ADR-011 / V7 operating truth).
LOW_MEAN_DB = -20.0
LOW_MAX_DB = -5.0


class AudioProfile:
    """Independent acoustic reference derived from ffmpeg filters.

    Attributes:
        mean_vol: mean volume in dB (None if undetermined).
        max_vol: max volume in dB (None if undetermined).
        silence_intervals: list of (start, end) seconds of detected silence.
        duration: media duration in seconds (None if undetermined).
        ok: False when ffmpeg failed or produced no usable signal.
    """

    def __init__(
        self,
        mean_vol: float | None = None,
        max_vol: float | None = None,
        silence_intervals: list[tuple[float, float]] | None = None,
        duration: float | None = None,
        ok: bool = True,
    ) -> None:
        self.mean_vol = mean_vol
        self.max_vol = max_vol
        self.silence_intervals = silence_intervals if silence_intervals is not None else []
        self.duration = duration
        self.ok = ok

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"AudioProfile(mean={self.mean_vol}, max={self.max_vol}, "
            f"silences={len(self.silence_intervals)}, dur={self.duration}, ok={self.ok})"
        )


_MEAN_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_MAX_RE = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_SIL_START_RE = re.compile(r"silence_start:\s*(\d+(?:\.\d+)?)")
_SIL_END_RE = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")


def parse_volumedetect(stderr: str) -> tuple[float | None, float | None]:
    """Parse mean/max volume (dB) from volumedetect stderr.

    Returns (mean_vol, max_vol); either may be None if absent.
    """
    mean_m = _MEAN_RE.search(stderr)
    max_m = _MAX_RE.search(stderr)
    mean = float(mean_m.group(1)) if mean_m else None
    maxv = float(max_m.group(1)) if max_m else None
    return mean, maxv


def probe_volume_window(path: str, start: float, end: float,
                        ff: str | None = None) -> tuple[float | None, float | None]:
    """volumedetect over ``[start, end)`` of ``path`` (ADR-031 D7).

    Used by verify to classify uncovered-audio windows against the demucs
    vocals track (BGM residue vs. real vocal energy) — the manual
    ``Temp\\check_windows.py`` adjudication workflow, productized.

    Returns ``(mean_db, max_db)``; either may be None when ffmpeg produced no
    parsable volume line. Raises on subprocess failure — the caller decides
    severity (the classification is advisory, never a gate).
    """
    if ff is None:
        ff = _resolve_binary("ffmpeg")
    dur = max(float(end) - float(start), 0.01)
    cmd = [ff, "-hide_banner", "-ss", str(float(start)), "-t", str(dur),
           "-i", path, "-af", "volumedetect", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return parse_volumedetect(proc.stderr)


def parse_silencedetect(stderr: str, duration: float | None = None) -> list[tuple[float, float]]:
    """Parse silence (start, end) intervals from silencedetect stderr.

    silencedetect emits `silence_start` then `silence_end` pairs. A trailing
    `silence_start` with no matching `silence_end` (silence to EOF) is closed at
    `duration` when known.
    """
    starts = [float(m) for m in _SIL_START_RE.findall(stderr)]
    ends = [float(m) for m in _SIL_END_RE.findall(stderr)]
    intervals: list[tuple[float, float]] = []
    # Pair up; ends may be one short when silence runs to EOF.
    for i, s in enumerate(starts):
        if i < len(ends):
            intervals.append((s, ends[i]))
        elif duration is not None:
            intervals.append((s, duration))
    return intervals


def recommend_vad(profile: AudioProfile) -> tuple[str, str]:
    """Advisory VAD suggestion from the audio profile (ADR-011 / ADR-012 / **ADR-034**).

    **ADR-034 (S2): this is advisory ONLY — it no longer drives routing.** The
    pipeline defaults to bare for every video, because a global ``--vad``
    *silently ejects speech that sits under laughter / cheering / BGM* (the
    5:52 miss; ADR-015 §Context documents the failure mode but only mitigated
    it down to a 240s-chunk granularity). E1 verification on ``IF.mp4``
    (vad vs bare) showed drift is bounded — word-level timestamps are
    near-identical and every segment boundary drifted < 0.3s — so the
    "anchor to silence" benefit does not justify that loss.

    Still returns ``(flag, rationale)`` so ``doctor`` can print the geometry for
    a human, but callers must NOT feed the flag into routing — see
    :func:`profile_recommendation`, which now hard-codes ``vad=False``.
    """
    if not profile.ok:
        return ("bare", "audio profile unavailable; default bare run (VAD off) is safest")
    mean, maxv = profile.mean_vol, profile.max_vol
    low = (mean is not None and mean < LOW_MEAN_DB) or (maxv is not None and maxv < LOW_MAX_DB)
    if low:
        return (
            "bare",
            f"low level (mean={mean}, max={maxv} dB); ADR-034 default bare — "
            f"no_speech=0.0 already keeps quiet speech; loudnorm / tuned VAD is "
            f"advisory only (not applied)",
        )
    # Normal level. ADR-034: do NOT return VAD — global VAD ejects
    # laughter-masked speech, which is strictly worse than the drift it prevents
    # (proven by E1: drift < 0.3s, i.e. bounded).
    return (
        "bare",
        f"clean level (mean={mean}, max={maxv} dB); ADR-034 default bare — VAD "
        f"anchoring is advisory only (global VAD ejects laughter-masked speech)",
    )


# A chunk whose silence coverage meets/exceeds this fraction is treated as
# "clean with clear pauses" -> VAD on anchors segment edges to real silence.
# Below it the chunk is continuous audio (laugh / cheer / music) -> bare, so
# VAD won't eject speech masked by the overlapping noise. (ADR-015)
CLEAN_SILENCE_FRACTION = 0.10


def _silence_fraction(silences: list[tuple[float, float]], dur: float) -> float:
    """Fraction of [0, dur] covered by detected silence intervals."""
    if not silences or dur <= 0:
        return 0.0
    covered = 0.0
    for (s, e) in silences:
        s = max(0.0, float(s))
        e = min(float(dur), float(e))
        if e > s:
            covered += (e - s)
    return covered / dur


def route_vad_chunk(profile: AudioProfile, chunk_dur: float) -> bool:
    """Per-chunk VAD routing for adaptive mode (ADR-015).

    Returns True if VAD should be ON for this chunk, False (bare) otherwise.
    Pure function of the chunk's local audio profile — deterministic, no I/O.
    """
    if not profile.ok:
        return False  # safe default: bare (don't drop anything)
    # Low level -> tuned VAD (recall for quiet speech) — ADR-011 low-level branch.
    low = (profile.mean_vol is not None and profile.mean_vol < LOW_MEAN_DB) or \
          (profile.max_vol is not None and profile.max_vol < LOW_MAX_DB)
    if low:
        return True
    # Otherwise: clean (clear pauses) anchors to silence; continuous noise -> bare.
    return _silence_fraction(profile.silence_intervals, chunk_dur) >= CLEAN_SILENCE_FRACTION


def analyze_audio(video_path: str, noise: str = "-30dB", d: float = 0.3) -> AudioProfile:
    """Run volumedetect + silencedetect and return an AudioProfile.

    Never raises for ffmpeg failure — returns AudioProfile(ok=False) so callers
    (doctor/verify) degrade gracefully instead of crashing the pipeline.
    """
    try:
        cmd = build_audio_profile_cmd(video_path, noise=noise, d=d)
        # Resolve the portable build even when called outside the CLI (toolchain
        # PATH injection not yet applied) — execution-time, command stays pure.
        cmd[0] = _resolve_binary("ffmpeg")
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            # Windows decodes with the locale codec (GBK on zh-CN) unless told
            # otherwise; ffmpeg echoes the input path, so a non-ASCII video name
            # raises UnicodeDecodeError and kills the whole profile pass.
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return AudioProfile(ok=False)
    if proc.returncode != 0:
        return AudioProfile(ok=False)
    stderr = proc.stderr
    mean, maxv = parse_volumedetect(stderr)
    silences = parse_silencedetect(stderr, duration=None)
    # ADR-034 (S2 / 一期): also probe media duration so G3 pre-screening and the
    # doctor print-out have a complete profile. Best-effort — a probe failure
    # leaves ``duration=None`` rather than failing the whole profile pass
    # (analyze_audio never raises for ffmpeg-side failures).
    duration = _probe_duration_best_effort(video_path)
    return AudioProfile(mean_vol=mean, max_vol=maxv,
                        silence_intervals=silences, duration=duration, ok=True)


def _probe_duration_best_effort(video_path: str) -> float | None:
    """Probe media duration, returning ``None`` instead of raising (ADR-034 一期).

    ``analyze_audio`` must never raise for ffmpeg-side failures, so a missing or
    failing ``ffprobe`` yields ``None`` (callers treat ``duration=None`` as
    "unknown geometry", exactly as before the wiring).
    """
    try:
        return probe_duration(video_path)
    except Exception:  # noqa: BLE001 - advisory only; never a gate
        return None


def probe_window_silence_fraction(
    video_path: str,
    start: float,
    end: float,
    *,
    noise: str = "-30dB",
    d: float = 0.3,
) -> float | None:
    """ADR-034 §6.3 组2(c)：对**单个候选窗**测静音占比（连续噪声/BGM 判据）。

    与整片画像（``analyze_audio`` 跑全片）不同，这是**窗级**探测，贴合
    §3.4「该窗所在 chunk 画像标 strong-BGM/continuous-noise」的表述。代价是
    每个候选窗一次短窗 silencedetect——G3 候选窗受性能预算封顶（≤10min，
    通常 1~3 窗），单次亚秒级，可忽略。

    返回窗内静音占比（0.0~1.0）；探测失败返回 ``None``（调用方预筛放行，
    交给 (a) demucs 能量复核兜底——双门设计）。
    """
    import tempfile

    chunk: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            chunk = tf.name
        extract_chunk(video_path, chunk, float(start),
                      max(float(end) - float(start), 0.01))
        prof = analyze_audio(chunk, noise=noise, d=d)
        if prof.ok and prof.duration:
            return _silence_fraction(prof.silence_intervals, prof.duration)
        return None
    except Exception:  # noqa: BLE001 - 预筛是粗门，失败不能拖垮 G3
        return None
    finally:
        try:
            if chunk and os.path.exists(chunk):
                os.remove(chunk)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# T5 / ADR-032: three-decision recommendation (the P0 -> P1 decision point)
# ---------------------------------------------------------------------------
# `doctor --video` and the Agent decision point must agree on the routing advice.
# Historically this logic lived inline in `cli.doctor` and was only printed, so
# the Agent had to re-derive it from prose — which is exactly how the P0 skip
# happened. It is now one pure function both callers share, and its output is
# persisted so every run can be audited.
@dataclass
class AudioProfileRecommendation:
    """The three routing decisions derived from an audio profile.

    These are exactly the three options the P0 -> P1 decision point puts to the
    user: translation style, VAD strategy, vocal separation.

    Attributes:
        style: film / literal / bilingual_study. User preference with no acoustic
            signal, so it always echoes the configured default.
        vad: enable global VAD (anchors segment boundaries to real silence).
        adaptive_vad: enable per-chunk VAD routing (mixed audio, ADR-015).
            Mutually exclusive with ``vad``.
        separate_vocals: run Demucs before transcription (strong BGM, ADR-017).
        vad_threshold: 0.1 for low-level audio, else None (= transcribe default).
        rationale: human-readable reason, surfaced at the decision point.
    """

    style: str = "film"
    vad: bool = False
    adaptive_vad: bool = False
    separate_vocals: bool = False
    vad_threshold: float | None = None
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form persisted into ``decisions.audio_profile``."""
        return {
            "style": self.style,
            "vad": self.vad,
            "adaptive_vad": self.adaptive_vad,
            "separate_vocals": self.separate_vocals,
            "vad_threshold": self.vad_threshold,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AudioProfileRecommendation":
        """Rebuild from a persisted dict; unknown keys are ignored."""
        return cls(
            style=data.get("style", "film"),
            vad=bool(data.get("vad", False)),
            adaptive_vad=bool(data.get("adaptive_vad", False)),
            separate_vocals=bool(data.get("separate_vocals", False)),
            vad_threshold=data.get("vad_threshold"),
            rationale=data.get("rationale", ""),
        )


def profile_recommendation(
    prof: AudioProfile | None,
    *,
    duration: float | None = None,
    default_style: str = "film",
    demucs_available: bool = True,
) -> AudioProfileRecommendation:
    """Derive the three routing decisions from an audio profile (ADR-032).

    Pure: no I/O, no subprocess, no environment reads, so ``doctor`` and the
    Agent decision point can share it and tests can drive it with synthetic
    profiles.

    Args:
        prof: result of :func:`analyze_audio`.
        duration: media duration in seconds. Needed to tell "clean with pauses"
            apart from "continuous noise"; without it the function stays
            conservative (no adaptive routing, no vocal separation).
        default_style: style to recommend (there is no acoustic signal for style).
        demucs_available: whether the Demucs binary resolves. Passed in rather
            than imported to keep this module free of a `vocal_sep` dependency.
    """
    if prof is None or not prof.ok:
        return AudioProfileRecommendation(
            style=default_style,
            vad=False,
            adaptive_vad=False,
            separate_vocals=False,
            vad_threshold=None,
            rationale=("audio profile unavailable (ffmpeg failed); "
                       "bare run is safest (VAD off)"),
        )

    # ADR-034 (S2 / 一期): the audio profile is now *advisory reference only* —
    # it no longer drives routing. Every video defaults to a bare run
    # (use_vad=False, no_speech=0.0) so a global ``--vad`` can never silently
    # eject speech sitting under laughter / cheer / BGM (the 5:52 miss;
    # ADR-015 §Context documents the failure mode). We still compute the silence
    # geometry for the doctor print-out and G3 pre-screening, but no routing flag
    # is switched on here; gradient recovery (G1/G2/G3) is driven by the
    # post-transcribe review (二期/三期), not by this gate.
    _, rationale = recommend_vad(prof)

    sf = _silence_fraction(prof.silence_intervals, duration) if duration else None
    continuous = sf is not None and sf < CLEAN_SILENCE_FRACTION

    vad = False
    adaptive_vad = False
    separate_vocals = False
    vad_threshold = None

    parts = [rationale]
    if sf is not None:
        parts.append(f"silence fraction {sf:.2f}")
        if continuous:
            detail = (f"continuous noise (< {CLEAN_SILENCE_FRACTION}) -> "
                      f"advisory only (recovery via G3 review, 二期/三期)")
            if demucs_available:
                detail += " (demucs available for G3)"
            parts.append(detail)
    else:
        parts.append("duration unknown -> continuous-noise detection skipped")
    if continuous and not demucs_available:
        parts.append("demucs not installed; G3 vocal separation unavailable")

    return AudioProfileRecommendation(
        style=default_style,
        vad=vad,
        adaptive_vad=adaptive_vad,
        separate_vocals=separate_vocals,
        vad_threshold=vad_threshold,
        rationale="; ".join(parts),
    )
