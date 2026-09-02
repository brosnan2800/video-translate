"""ADR-032 / ADR-034 (S2, 一期): the three-decision recommendation pure function.

ADR-034 (S2, 一期) downgrades the audio profile to an *advisory reference only*:
``profile_recommendation`` must NOT drive routing — every video defaults to a
bare run. These tests assert that contract (``vad`` / ``adaptive_vad`` /
``separate_vocals`` are all ``False`` regardless of geometry) while still
verifying the silence geometry is computed for the doctor print-out. They also
cover the ``analyze_audio`` duration wiring (一期): duration is threaded through
to the profile, and an ffprobe failure degrades to ``None`` instead of raising.
"""
from unittest.mock import patch

from video_translate.audio_profile import (
    AudioProfile,
    AudioProfileRecommendation,
    analyze_audio,
    profile_recommendation,
)


def _p(mean: float, maxv: float, silences) -> AudioProfile:
    return AudioProfile(
        mean_vol=mean, max_vol=maxv, silence_intervals=silences, ok=True
    )


def test_clean_recommendation_is_bare():
    # ADR-034: clean studio geometry no longer triggers global VAD.
    rec = profile_recommendation(
        _p(-14.0, -2.0, [(1, 3), (10, 14), (20, 25)]), duration=60.0
    )
    assert rec.vad is False
    assert rec.adaptive_vad is False
    assert rec.separate_vocals is False
    assert rec.vad_threshold is None


def test_dense_continuous_recommendation_is_bare():
    # Continuous (laughter/BGM) audio is advisory only; G3 review handles it (三期).
    rec = profile_recommendation(_p(-8.0, -1.0, [(1, 1.2)]), duration=60.0)
    assert rec.vad is False
    assert rec.adaptive_vad is False
    assert rec.separate_vocals is False


def test_low_level_recommendation_is_bare():
    # Low level no longer asks for tuned VAD; bare keeps quiet speech (no_speech=0.0).
    rec = profile_recommendation(
        _p(-28.0, -9.0, [(1, 3), (10, 14)]), duration=60.0
    )
    assert rec.vad is False
    assert rec.vad_threshold is None


def test_failed_profile_is_bare():
    rec = profile_recommendation(None)
    assert rec.vad is False
    assert rec.adaptive_vad is False
    assert rec.separate_vocals is False
    assert "bare run" in rec.rationale


def test_recommendation_keeps_geometry_in_rationale():
    # The profile is still useful as a reference: doctor prints silence geometry.
    rec = profile_recommendation(_p(-8.0, -1.0, [(1, 1.2)]), duration=60.0)
    assert "silence fraction" in rec.rationale
    assert "continuous" in rec.rationale


def test_style_always_defaults_to_config():
    rec = profile_recommendation(_p(-14.0, -2.0, [(1, 3)]), duration=60.0,
                                 default_style="literal")
    assert rec.style == "literal"


def test_rationale_nonempty():
    rec = profile_recommendation(_p(-14.0, -2.0, [(1, 3)]), duration=60.0)
    assert rec.rationale


def test_recommendation_roundtrip():
    rec = profile_recommendation(_p(-14.0, -2.0, [(1, 3)]), duration=60.0)
    d = rec.to_dict()
    rec2 = AudioProfileRecommendation.from_dict(d)
    assert rec2.style == rec.style
    assert rec2.vad == rec.vad
    assert rec2.adaptive_vad == rec.adaptive_vad
    assert rec2.separate_vocals == rec.separate_vocals
    assert rec2.vad_threshold == rec.vad_threshold
    assert rec2.rationale == rec.rationale


def test_analyze_audio_probes_duration():
    # ADR-034 (一期): duration is threaded into the profile.
    fake_stderr = "mean_volume: -20.0 dB\nmax_volume: -5.0 dB\n"
    fake_proc = __import__("subprocess").CompletedProcess(
        args=[], returncode=0, stdout="", stderr=fake_stderr
    )
    with patch("video_translate.audio_profile.subprocess.run",
               return_value=fake_proc), \
         patch("video_translate.audio_profile._resolve_binary",
               return_value="ffmpeg"), \
         patch("video_translate.audio_profile.probe_duration", return_value=131.2):
        prof = analyze_audio("dummy.mp4")
    assert prof.ok is True
    assert prof.mean_vol == -20.0
    assert prof.duration == 131.2


def test_analyze_audio_duration_probe_failure_is_nonfatal():
    # ffprobe failure must degrade to duration=None, never raise.
    fake_stderr = "mean_volume: -20.0 dB\nmax_volume: -5.0 dB\n"
    fake_proc = __import__("subprocess").CompletedProcess(
        args=[], returncode=0, stdout="", stderr=fake_stderr
    )
    with patch("video_translate.audio_profile.subprocess.run",
               return_value=fake_proc), \
         patch("video_translate.audio_profile._resolve_binary",
               return_value="ffmpeg"), \
         patch("video_translate.audio_profile.probe_duration",
               side_effect=RuntimeError("ffprobe missing")):
        prof = analyze_audio("dummy.mp4")
    assert prof.ok is True
    assert prof.duration is None
