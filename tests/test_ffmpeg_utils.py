"""Unit tests for ffmpeg command construction (Spec 02)."""
from video_translate.ffmpeg_utils import build_extract_cmd, build_probe_cmd


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
