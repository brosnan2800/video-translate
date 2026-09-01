"""Unit tests for ffmpeg command construction (Spec 02)."""
import os
import shutil

from pathlib import Path

from video_translate.ffmpeg_utils import (
    _resolve_binary,
    build_extract_cmd,
    build_probe_cmd,
)


def test_probe_cmd_shape():
    cmd = build_probe_cmd("in.mp4")
    assert cmd[0] == "ffprobe"
    assert "format=duration" in cmd
    assert cmd[-1] == "in.mp4"


def test_extract_cmd_16khz_mono_wav():
    cmd = build_extract_cmd("in.mp4", "out.wav", 12.0, 240.0)
    assert cmd[0] == "ffmpeg"
    # 16kHz mono is what Whisper expects.
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "16000"
    assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1"
    assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "12.0"
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "240.0"
    assert cmd[-1] == "out.wav"


# ---------------------------------------------------------------------------
# Control plane §2.3: tool resolution never silently depends on process PATH
# ---------------------------------------------------------------------------

def test_resolve_binary_prefers_persisted_toolchain_path(monkeypatch):
    """Consume init_toolchain's cached absolute path instead of a per-call
    shutil.which — the root-cause fix for fill_gaps/verify losing ffmpeg."""
    from video_translate import toolchain

    status = toolchain.ToolchainStatus()
    status.ffmpeg_path = str(Path("C:/virtual/tools/ffmpeg.exe"))
    status.ffprobe_path = str(Path("C:/virtual/tools/ffprobe.exe"))
    monkeypatch.setattr(toolchain, "get_toolchain_status", lambda: status)

    assert _resolve_binary("ffmpeg") == "C:\\virtual\\tools\\ffmpeg.exe"
    assert _resolve_binary("ffprobe") == "C:\\virtual\\tools\\ffprobe.exe"


def test_resolve_binary_falls_back_to_which_or_bare(monkeypatch):
    from video_translate import toolchain

    status = toolchain.ToolchainStatus()  # no paths persisted
    monkeypatch.setattr(toolchain, "get_toolchain_status", lambda: status)
    monkeypatch.setattr(shutil, "which", lambda name, **kw: None)

    assert _resolve_binary("ffmpeg") == "ffmpeg"  # bare name preserved


def test_resolve_binary_self_heals_bypassing_main(tmp_path, monkeypatch):
    """§2.3 contract: a direct library call that never went through main() /
    init_toolchain still resolves the portable binary via lazy init — no
    FileNotFoundError, no dependence on the caller's CWD."""
    from video_translate import ffmpeg_utils, toolchain

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    fake.write_bytes(b"MZ")
    (tmp_path / ".env").write_text(
        f"VT_FFMPEG_DIR={bin_dir}\n", encoding="utf-8"
    )

    # Simulate a cold process that skipped main()/init_toolchain entirely,
    # with no ffmpeg anywhere on PATH.
    monkeypatch.setattr(toolchain, "_GLOBAL_TOOLCHAIN", None)
    monkeypatch.setattr(toolchain, "project_root", lambda: str(tmp_path))
    monkeypatch.setenv("PATH", "")

    resolved = ffmpeg_utils._resolve_binary("ffmpeg")
    assert os.path.normcase(resolved) == os.path.normcase(str(fake))
    # Second call reuses the persisted path (stable across pipeline stages).
    assert os.path.normcase(ffmpeg_utils._resolve_binary("ffmpeg")) == os.path.normcase(str(fake))
