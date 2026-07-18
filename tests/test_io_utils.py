"""Unit tests for io_utils atomic/resume-safe I/O (Spec 07, Gotcha 4)."""
import json
import os

import pytest

from video_translate.io_utils import (
    load_json,
    load_json_default,
    save_json,
    write_text,
)


def test_save_and_load_roundtrip(tmp_path):
    p = os.path.join(tmp_path, "a.json")
    save_json(p, {"k": "值"})
    assert load_json(p) == {"k": "值"}


def test_save_json_is_utf8_not_ascii_escaped(tmp_path):
    p = os.path.join(tmp_path, "cn.json")
    save_json(p, {"x": "长鑫"})
    with open(p, "r", encoding="utf-8") as f:
        raw = f.read()
    assert "长鑫" in raw  # ensure_ascii=False


def test_load_json_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_json(os.path.join(tmp_path, "nope.json"))


def test_load_json_default_missing_returns_default(tmp_path):
    assert load_json_default(os.path.join(tmp_path, "nope.json"), {}) == {}


def test_load_json_default_corrupt_returns_default(tmp_path):
    p = os.path.join(tmp_path, "bad.json")
    with open(p, "w") as f:
        f.write("{not valid")
    assert load_json_default(p, []) == []


def test_write_text_atomic_roundtrip(tmp_path):
    p = os.path.join(tmp_path, "t.txt")
    write_text(p, "hello\n世界\n")
    with open(p, "r", encoding="utf-8") as f:
        assert f.read() == "hello\n世界\n"


def test_save_json_leaves_no_temp_file(tmp_path):
    p = os.path.join(tmp_path, "x.json")
    save_json(p, {"a": 1})
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []
