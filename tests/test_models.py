"""Unit tests for the Segment model (Spec 01)."""
import pytest

from video_translate.models import Segment


def test_from_dict_basic():
    s = Segment.from_dict({"start": 1.0, "end": 2.5, "text": "  hi  "})
    assert s.start == 1.0 and s.end == 2.5
    assert s.text == "hi"  # stripped


def test_from_dict_missing_text_defaults_empty():
    s = Segment.from_dict({"start": 0, "end": 1})
    assert s.text == ""


def test_from_dict_missing_start_raises():
    with pytest.raises(KeyError):
        Segment.from_dict({"end": 1, "text": "x"})


def test_from_dict_missing_end_raises():
    with pytest.raises(KeyError):
        Segment.from_dict({"start": 1, "text": "x"})


def test_to_dict_rounds_to_two_decimals():
    s = Segment(start=1.23456, end=2.98765, text="z")
    assert s.to_dict() == {"start": 1.23, "end": 2.99, "text": "z"}
