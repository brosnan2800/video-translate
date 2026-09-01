"""Tests for the control-plane capability probes (§2) and GateFail (exit 8).

Every probe must be deterministic in unit tests — no network, no real
ffmpeg/probe runs; capabilities that touch the machine are monkeypatched.
"""
import os
from pathlib import Path

import pytest

from video_translate.capabilities import (
    CAPS,
    _CAP_BY_NAME,
    GateFail,
    probe,
    probe_all,
    require,
    require_all,
)


def test_caps_cover_expected_names():
    names = {c.name for c in CAPS}
    assert {
        "ffmpeg", "ffprobe", "model:large-v3", "cuda", "whisperx", "demucs",
    } <= names
    # registration is keyed consistently
    assert set(_CAP_BY_NAME) == names


def test_probe_all_returns_every_capability():
    snapshot = probe_all()
    assert set(snapshot) == set(_CAP_BY_NAME)
    assert all(isinstance(v, bool) for v in snapshot.values())


def test_probe_unknown_raises_gatefail():
    with pytest.raises(GateFail):
        probe("no-such-capability")


def test_require_passes_when_available(monkeypatch):
    monkeypatch.setattr(
        "video_translate.capabilities._CAP_BY_NAME",
        {"ffmpeg": [c for c in CAPS if c.name == "ffmpeg"][0]},
        raising=False,
    )
    # ffmpeg is available on the host (per doctor); still keep it hermetic:
    monkeypatch.setattr("video_translate.capabilities.probe", lambda n: True)
    require("ffmpeg")  # should not raise


def test_require_raises_gatefail_with_guidance(monkeypatch):
    monkeypatch.setattr("video_translate.capabilities.probe", lambda n: False)
    with pytest.raises(GateFail) as exc:
        require("ffmpeg")
    assert "ffmpeg" in exc.value.message
    assert "setup --ffmpeg" in exc.value.guidance


def test_require_all_raises_when_a_core_cap_missing(monkeypatch):
    monkeypatch.setattr("video_translate.capabilities.probe", lambda n: False)
    with pytest.raises(GateFail):
        require_all()


def test_ffmpeg_probe_uses_persisted_tool_absolute_path(tmp_path, monkeypatch):
    """§2.3 integration: the ffmpeg capability probe is backed by the persisted
    toolchain path (resolve_tool), not a fresh per-call PATH search."""
    from video_translate import toolchain
    from video_translate.capabilities import _probe_ffmpeg

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    exe.write_bytes(b"MZ")
    (tmp_path / ".env").write_text(
        f"VT_FFMPEG_DIR={bin_dir}\n", encoding="utf-8"
    )

    monkeypatch.setattr(toolchain, "_GLOBAL_TOOLCHAIN", None)
    monkeypatch.setattr(toolchain, "project_root", lambda: str(tmp_path))
    monkeypatch.setenv("PATH", "")

    assert _probe_ffmpeg() is True


def test_gatefail_default_guidance_empty():
    err = GateFail("boom")
    assert err.message == "boom"
    assert err.guidance == ""
