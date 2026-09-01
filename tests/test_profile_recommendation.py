"""ADR-032: the three-decision recommendation pure function.

Covers the four audio-profile geometries, structural completeness, and the
to_dict / from_dict round-trip used when persisting into ``vt_state.json``.
"""
from video_translate.audio_profile import (
    AudioProfile,
    AudioProfileRecommendation,
    profile_recommendation,
)


def _p(mean: float, maxv: float, silences) -> AudioProfile:
    return AudioProfile(
        mean_vol=mean, max_vol=maxv, silence_intervals=silences, ok=True
    )


def test_clean_recommendation():
    rec = profile_recommendation(
        _p(-14.0, -2.0, [(1, 3), (10, 14), (20, 25)]), duration=60.0
    )
    assert rec.vad is True
    assert rec.adaptive_vad is False
    assert rec.separate_vocals is False
    assert rec.vad_threshold is None


def test_dense_continuous_recommendation():
    # Clean level but almost no silence -> continuous noise -> adaptive + separation.
    rec = profile_recommendation(_p(-8.0, -1.0, [(1, 1.2)]), duration=60.0)
    assert rec.adaptive_vad is True
    assert rec.vad is False
    assert rec.separate_vocals is True


def test_low_level_recommendation():
    rec = profile_recommendation(
        _p(-28.0, -9.0, [(1, 3), (10, 14)]), duration=60.0
    )
    assert rec.vad is True
    assert rec.vad_threshold == 0.1  # loudnorm + relaxed VAD threshold


def test_failed_profile_is_bare():
    rec = profile_recommendation(None)
    assert rec.vad is False
    assert rec.adaptive_vad is False
    assert rec.separate_vocals is False
    assert "bare run" in rec.rationale


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
