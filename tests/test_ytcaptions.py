"""Spec 29 / ADR-042 — 接口型 ASR 的句子化纯函数测试（离线，零网络）。

只覆盖 `ytcaptions` 的**纯函数层**：URL 解析 / 去重叠拼接 / 切句 / 时间戳插值。
抓取与预检（网络层）在 `test_captions_cli.py` 里用 mock 覆盖。
"""
from __future__ import annotations

import pytest

from video_translate.ytcaptions import (
    accumulate,
    parse_video_id,
    snippets_to_segments,
    split_points,
)

# --------------------------------------------------------------------------- #
# parse_video_id
# --------------------------------------------------------------------------- #


def test_parse_bare_id():
    assert parse_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/v/dQw4w9WgXcQ",
    "  https://youtu.be/dQw4w9WgXcQ  ",
])
def test_parse_url_forms(url):
    assert parse_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("bad", ["", "   ", "https://example.com/nope", "short"])
def test_parse_invalid_raises(bad):
    with pytest.raises(ValueError):
        parse_video_id(bad)


# --------------------------------------------------------------------------- #
# accumulate（去重叠拼接）
# --------------------------------------------------------------------------- #


def test_accumulate_strips_rolling_overlap():
    """YouTube 自动轨的典型形态：相邻片段重复上一条的尾巴。"""
    snips = [
        {"text": "so today", "start": 0.0, "duration": 1.0},
        {"text": "today we're", "start": 0.8, "duration": 1.0},
        {"text": "we're going", "start": 1.6, "duration": 1.0},
        {"text": "going to talk about", "start": 2.4, "duration": 1.5},
    ]
    text, owners = accumulate(snips)
    assert text == "so today we're going to talk about"
    assert len(owners) == len(text)
    assert owners[0] == 0 and owners[-1] == 3


def test_accumulate_joins_non_overlapping_with_space():
    """无重叠的片段之间补空格，避免词被粘死。"""
    text, _ = accumulate([
        {"text": "Hello", "start": 0.0, "duration": 1.0},
        {"text": "world.", "start": 1.0, "duration": 1.0},
    ])
    assert text == "Hello world."


def test_accumulate_skips_blank_and_normalizes_newlines():
    text, _ = accumulate([
        {"text": "  ", "start": 0.0, "duration": 1.0},
        {"text": "a\nb", "start": 1.0, "duration": 1.0},
    ])
    assert text == "a b"


# --------------------------------------------------------------------------- #
# split_points（切句）
# --------------------------------------------------------------------------- #


def test_split_ascii_sentence_end():
    # 切点 = 标点之后、**跳过连续标点与空白**的位置（下一句不以空格开头）
    assert split_points("One. Two! Three?") == [5, 10, 16]


def test_split_cjk_punctuation():
    assert split_points("今天很好。明天呢？") == [5, 9]


def test_split_does_not_break_decimals():
    """小数里的 `.` 不是句末（前后都是数字）。"""
    assert split_points("It is 3.14 meters") == []


def test_split_does_not_break_abbreviations():
    """`Mr. Smith` 不能被腰斩（短缩写点）。"""
    assert split_points("Mr. Smith went home.") == [20]
    assert split_points("Dr. Who and Mr. X") == []


def test_split_merges_consecutive_punctuation():
    """`...` / `?!` 合并成一个边界。"""
    assert split_points("Really?! Yes...") == [9, 15]


# --------------------------------------------------------------------------- #
# snippets_to_segments（句子化主入口）
# --------------------------------------------------------------------------- #

_ROLLING = [
    {"text": "so today", "start": 0.0, "duration": 1.0},
    {"text": "today we're", "start": 0.8, "duration": 1.0},
    {"text": "we're going", "start": 1.6, "duration": 1.0},
    {"text": "going to talk about pricing.", "start": 2.4, "duration": 1.6},
    {"text": "That's the plan!", "start": 4.2, "duration": 1.2},
]


def test_snippets_to_segments_sentence_izes():
    segs = snippets_to_segments(_ROLLING)
    assert [s["text"] for s in segs] == [
        "so today we're going to talk about pricing.",
        "That's the plan!",
    ]


def test_segments_have_monotonic_non_overlapping_windows():
    """load-bearing 不变量：start < end、严格非递减、互不重叠。"""
    segs = snippets_to_segments(_ROLLING)
    assert segs
    prev_end = None
    for s in segs:
        assert s["start"] < s["end"]
        if prev_end is not None:
            assert s["start"] >= prev_end
        prev_end = s["end"]


def test_segment_window_comes_from_first_and_last_snippet():
    """start 取首片段、end 取末片段（Spec 29 的插值口径）。"""
    segs = snippets_to_segments(_ROLLING)
    assert segs[0]["start"] == 0.0
    # 第一句覆盖到第 4 条片段结束：2.4 + 1.6
    assert segs[0]["end"] == pytest.approx(4.0)
    # 第二句从第 5 条片段开始
    assert segs[1]["start"] == pytest.approx(4.2)


def test_text_is_single_line():
    """ADR-040：产出必须是单行文本。"""
    segs = snippets_to_segments([
        {"text": "line one\nline two.", "start": 0.0, "duration": 1.0},
    ])
    assert all("\n" not in s["text"] and "\r" not in s["text"] for s in segs)


def test_no_punctuation_falls_back_to_soft_limit():
    """整段没有句末标点时，按软上限强制切分（防"整篇一句"）。"""
    long_text = " ".join(["word"] * 60)          # 无标点，远超 2 × max_chars
    segs = snippets_to_segments(
        [{"text": long_text, "start": 0.0, "duration": 30.0}],
        max_chars=42, max_dur=6.0,
    )
    assert len(segs) > 1
    assert all(s["start"] < s["end"] for s in segs)
    assert all(len(s["text"]) <= 42 * 2 for s in segs)


def test_empty_input_yields_no_segments():
    assert snippets_to_segments([]) == []
    assert snippets_to_segments([{"text": "   ", "start": 0.0, "duration": 1.0}]) == []
