"""ADR-032: P0->P1 run gate — auto-profile fallback + three-decision persistence.

Exercises ``cli._resolve_routing``: no snapshot auto-profiles & persists, an
existing snapshot is reused, an existing routing is honoured (origin graded),
an explicit CLI flag overrides, and ``--require-profile`` hard-gates.

``analyze_audio`` is stubbed so the tests stay deterministic and ffmpeg-free;
the recommendation geometry itself is covered in ``test_profile_recommendation``.
"""
import argparse

import pytest

from video_translate import cli, state
from video_translate.audio_profile import AudioProfileRecommendation


@pytest.fixture
def no_ffmpeg(monkeypatch):
    """Make analyze_audio always "fail" (None) — bare fallback recommendation."""
    monkeypatch.setattr(
        "video_translate.audio_profile.analyze_audio",
        lambda *a, **k: None,
    )


def _ns(**kw):
    base = dict(vad=False, adaptive_vad=False, separate_vocals=False,
                vad_threshold=None, style=None, require_profile=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _cfg(style: str = "film"):
    from video_translate.config import Config

    return Config(style=style)


def _routing_origin(outdir: str, base: str) -> str:
    st = state.load(outdir, base)
    return st["decisions"]["routing"]["origin"]


def test_no_snapshot_auto_profiles_and_persists(tmp_path, no_ffmpeg):
    """No snapshot -> auto-profile (bare) + persist audio_profile & routing."""
    final, origin = cli._resolve_routing(
        _ns(), str(tmp_path), "vid", "missing.mp4", _cfg()
    )
    assert final is not None
    assert origin == "profile"
    assert state.get_audio_profile(str(tmp_path), "vid") is not None
    assert state.get_routing(str(tmp_path), "vid") is not None
    assert _routing_origin(str(tmp_path), "vid") == "profile"


def test_has_snapshot_reuses(tmp_path, no_ffmpeg):
    """An existing snapshot is reused; final routing reflects its recommendation."""
    rec = AudioProfileRecommendation(
        style="film", vad=True, vad_threshold=0.1, rationale="x"
    )
    state.record_audio_profile(str(tmp_path), "vid", rec)
    final, origin = cli._resolve_routing(
        _ns(), str(tmp_path), "vid", "missing.mp4", _cfg()
    )
    assert final["vad"] is True
    assert final["vad_threshold"] == 0.1
    assert origin == "profile"


def test_routing_origin_explicit_is_respected(tmp_path, no_ffmpeg):
    """Existing explicit routing wins; origin stays explicit."""
    state.record_routing(
        str(tmp_path), "vid", style="literal", vad=True,
        adaptive_vad=False, separate_vocals=True, vad_threshold=None,
        origin="explicit",
    )
    final, origin = cli._resolve_routing(
        _ns(), str(tmp_path), "vid", "x.mp4", _cfg()
    )
    assert final["style"] == "literal"
    assert final["separate_vocals"] is True
    assert origin == "explicit"


def test_cli_flag_overrides_routing(tmp_path, no_ffmpeg):
    """An explicit CLI flag overrides a persisted routing -> origin explicit."""
    state.record_routing(
        str(tmp_path), "vid", style="literal", vad=False,
        adaptive_vad=False, separate_vocals=False, vad_threshold=None,
        origin="explicit",
    )
    final, origin = cli._resolve_routing(
        _ns(vad=True), str(tmp_path), "vid", "x.mp4", _cfg()
    )
    assert final["vad"] is True
    assert origin == "explicit"


def test_require_profile_hard_gate_blocks_without_explicit(tmp_path, no_ffmpeg):
    """--require-profile with no explicit routing -> gate fails (None returned)."""
    final, origin = cli._resolve_routing(
        _ns(require_profile=True), str(tmp_path), "vid", "x.mp4", _cfg()
    )
    assert final is None
    assert origin == "explicit"


def test_require_profile_passes_with_explicit_routing(tmp_path, no_ffmpeg):
    """--require-profile passes when an explicit routing already exists."""
    state.record_routing(
        str(tmp_path), "vid", style="film", vad=False,
        adaptive_vad=False, separate_vocals=False, vad_threshold=None,
        origin="explicit",
    )
    final, origin = cli._resolve_routing(
        _ns(require_profile=True), str(tmp_path), "vid", "x.mp4", _cfg()
    )
    assert final is not None
