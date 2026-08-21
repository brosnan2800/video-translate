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

import re
import subprocess

from .ffmpeg_utils import build_audio_profile_cmd

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
    """Recommend a VAD setting from the audio profile (ADR-011 / ADR-012).

    Returns (flag, rationale):
      - ("--vad", ...)               clean studio / recitation -> anchor to silence
      - ("--vad --vad-threshold 0.1", ...)  low level -> normalize first, tuned VAD
      - ("bare", ...)                music-heavy / low-SNR / whisper -> default off
    """
    if not profile.ok:
        return ("bare", "audio profile unavailable; default bare run (VAD off) is safest")
    mean, maxv = profile.mean_vol, profile.max_vol
    low = (mean is not None and mean < LOW_MEAN_DB) or (maxv is not None and maxv < LOW_MAX_DB)
    if low:
        return (
            "--vad --vad-threshold 0.1",
            f"low level (mean={mean}, max={maxv} dB) -> loudnorm first, then tuned VAD",
        )
    # Normal level. Distingushing music-heavy (level reads normal but VAD drops
    # speech) from clean studio needs spectral analysis we don't do here; the
    # safe default for a normal-level clip is clean-studio VAD (anchors segment
    # boundaries to real silence, kills drift). Agent may override to bare for
    # known music-heavy content.
    return ("--vad", f"clean level (mean={mean}, max={maxv} dB) -> VAD anchors to silence")


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
        proc = subprocess.run(
            build_audio_profile_cmd(video_path, noise=noise, d=d),
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return AudioProfile(ok=False)
    if proc.returncode != 0:
        return AudioProfile(ok=False)
    stderr = proc.stderr
    mean, maxv = parse_volumedetect(stderr)
    # Best-effort duration from ffprobe-style line is not emitted by this pass;
    # leave duration None. Callers that need it can probe separately.
    silences = parse_silencedetect(stderr, duration=None)
    return AudioProfile(mean_vol=mean, max_vol=maxv, silence_intervals=silences, ok=True)
