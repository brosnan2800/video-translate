"""Unit tests for srt_utils (Spec 04, Gotcha 8)."""
from video_translate.srt_utils import block, srt_time


def test_srt_time_basic():
    assert srt_time(0) == "00:00:00,000"
    assert srt_time(1.5) == "00:00:01,500"
    assert srt_time(61.25) == "00:01:01,250"
    assert srt_time(3661.001) == "01:01:01,001"


def test_srt_time_negative_clamps_to_zero():
    assert srt_time(-5) == "00:00:00,000"


def test_srt_time_ms_carry():
    # round(0.9999*1000) == 1000 must carry into the next second, not ",1000".
    assert srt_time(0.9999) == "00:00:01,000"
    assert srt_time(59.9999) == "00:01:00,000"


def test_block_two_lines():
    b = block(3, 1.0, 2.0, ["中文", "English"])
    assert b == "3\n00:00:01,000 --> 00:00:02,000\n中文\nEnglish\n"


def test_block_single_line():
    b = block(1, 0.0, 1.0, ["only"])
    assert b == "1\n00:00:00,000 --> 00:00:01,000\nonly\n"
