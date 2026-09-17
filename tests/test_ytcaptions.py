"""Spec 29 / ADR-042 — 接口型 ASR 的句子化纯函数测试（离线，零网络）。

只覆盖 `ytcaptions` 的**纯函数层**：URL 解析 / 去重叠拼接 / 切句 / 时间戳插值。
抓取与预检（网络层）在 `test_captions_cli.py` 里用 mock 覆盖。
"""
from __future__ import annotations

import pytest

from video_translate.ytcaptions import (
    INPUT_PATH,
    INPUT_URL_UNSUPPORTED,
    INPUT_YOUTUBE,
    accumulate,
    classify_input,
    effective_windows,
    looks_like_url,
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
    """start 取首片段、end 取**末片段窗口的终点**（Spec 29 的插值口径）。"""
    segs = snippets_to_segments(_ROLLING)
    assert segs[0]["start"] == 0.0
    # 第一句的窗口止于**第 5 条片段的 start（4.2）** —— 即滚动轨的真实换行时刻，
    # 而不是第 4 条片段的 start+duration（4.0，那是"显示时长"，会越过换行点）。
    assert segs[0]["end"] == pytest.approx(4.2)
    assert segs[1]["start"] == segs[0]["end"]
    assert segs[1]["end"] == pytest.approx(5.4)


# --- 滚动窗口模型（实测事故：duration 是显示时长，会越过下一条的 start） ---


def test_effective_windows_stops_at_next_start():
    """滚动轨：片段 k 的有效窗口止于片段 k+1 的 start。"""
    assert effective_windows([
        {"text": "a", "start": 0.0, "duration": 3.0},
        {"text": "b", "start": 2.0, "duration": 2.0},
    ]) == [(0.0, 2.0), (2.0, 4.0)]


def test_effective_windows_keeps_non_overlapping_data():
    """非滚动数据不受影响：窗口仍是 ``[start, start+duration]``（留出真实间隙）。"""
    assert effective_windows([
        {"text": "a", "start": 0.0, "duration": 1.0},
        {"text": "b", "start": 4.0, "duration": 1.0},
    ]) == [(0.0, 1.0), (4.0, 5.0)]


def test_two_sentences_in_one_snippet_are_not_squeezed():
    """实测事故（Shorts 视频）：一条片段含两句 → 第二句被挤成 0.01s 不可见微段。

    成因是「窗口 = start + duration」+「互不重叠」钳制：第一句占满整个显示窗口，
    第二句无处可放。正解是按**字符长度比例**分摊窗口（ADR-042 D2 的「成比例」）。
    """
    segs = snippets_to_segments([
        {"text": "Just forgive. And don't worry.", "start": 0.0, "duration": 3.0},
        {"text": "Next.", "start": 2.0, "duration": 2.0},
    ])
    assert [s["text"] for s in segs] == [
        "Just forgive.", "And don't worry.", "Next."]
    # 关键断言：没有 0.01s 微段
    assert all(s["end"] - s["start"] > 0.3 for s in segs)
    # 比例性：第二句（16 字符）分到的窗口应长于第一句（13 字符）
    d0 = segs[0]["end"] - segs[0]["start"]
    d1 = segs[1]["end"] - segs[1]["start"]
    assert d1 > d0


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


# --------------------------------------------------------------------------- #
# _pick_transcript：只有自动轨时必须回退（实测事故）
# --------------------------------------------------------------------------- #

class _FakeTranscriptList:
    """模拟 ``TranscriptList`` 的**关键语义**：``find_*`` 无匹配时**抛异常**，
    不是返回 ``None``（见库的 ``_find_transcript``）。"""

    def __init__(self, manual=(), generated=("en",)):
        self._manual, self._generated = set(manual), set(generated)

    def _find(self, langs, pool, label):
        from youtube_transcript_api import NoTranscriptFound

        for lang in langs:
            if lang in pool:
                return {"lang": lang, "track": label}
        raise NoTranscriptFound("vid", list(langs), None)

    def find_manually_created_transcript(self, langs):
        return self._find(langs, self._manual, "manual")

    def find_generated_transcript(self, langs):
        return self._find(langs, self._generated, "auto")


def test_pick_transcript_falls_back_to_auto_when_no_manual():
    """实测事故：视频只有自动轨时，人工轨查找抛 ``NoTranscriptFound`` 被误判成
    「整个视频没有字幕」—— 而 ``--list`` 明明列得出轨道（Shorts 视频即如此）。

    修法：逐类轨道接住异常再试下一类。
    """
    from video_translate.ytcaptions import _pick_transcript

    picked = _pick_transcript(_FakeTranscriptList(manual=(), generated=("en",)),
                              ["en"], allow_auto=True)
    assert picked == {"lang": "en", "track": "auto"}


def test_pick_transcript_prefers_manual_over_auto():
    from video_translate.ytcaptions import _pick_transcript

    picked = _pick_transcript(_FakeTranscriptList(manual=("en",),
                                                  generated=("en",)),
                              ["en"], allow_auto=True)
    assert picked["track"] == "manual"


def test_pick_transcript_no_auto_excludes_generated():
    """``--no-auto`` 时自动轨被排除 → 明确报「没有可用字幕」（不静默接受自动轨）。"""
    from video_translate.ytcaptions import CaptionUnavailable, _pick_transcript

    with pytest.raises(CaptionUnavailable):
        _pick_transcript(_FakeTranscriptList(manual=(), generated=("en",)),
                         ["en"], allow_auto=False)


def test_pick_transcript_language_priority():
    from video_translate.ytcaptions import _pick_transcript

    picked = _pick_transcript(_FakeTranscriptList(manual=("ja", "en")),
                              ["en", "ja"], allow_auto=True)
    assert picked["lang"] == "en"


# --------------------------------------------------------------------------- #
# 输入形态判定（ADR-043 D2）—— 全项目唯一的判定来源
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", [
    "https://www.youtube.com/watch?v=GxggU7XoCLg",
    "http://youtube.com/watch?v=GxggU7XoCLg&t=30",
    "https://youtu.be/GxggU7XoCLg?t=30",
    "https://m.youtube.com/watch?v=GxggU7XoCLg",
    "https://music.youtube.com/watch?v=GxggU7XoCLg",
    "https://www.youtube.com/shorts/GxggU7XoCLg",
    "https://www.youtube.com/embed/GxggU7XoCLg",
    "https://www.youtube-nocookie.com/embed/GxggU7XoCLg",
    "HTTPS://WWW.YOUTUBE.COM/watch?v=GxggU7XoCLg",   # scheme/host 大小写无关
])
def test_classify_input_youtube(raw):
    assert classify_input(raw) == INPUT_YOUTUBE


@pytest.mark.parametrize("raw", [
    "https://www.bilibili.com/video/BV1xx411c7mD",
    "https://vimeo.com/123456789",
    "https://example.com/abcdefghijk",    # 末段恰 11 字符：host 判定必须挡住
    "https://www.youtube.com/",           # 油管域名但没有视频 id
    "https://notyoutube.com/watch?v=GxggU7XoCLg",   # 域名后缀伪装
])
def test_classify_input_url_unsupported(raw):
    assert classify_input(raw) == INPUT_URL_UNSUPPORTED


@pytest.mark.parametrize("raw", [
    "videos/a.mp4",
    "C:/videos/a.mp4",
    r"C:\videos\a.mp4",
    r"C://videos/a.mp4",                  # 手滑双斜杠：scheme ≥2 字符收紧挡住
    r"\\server\share\a.mp4",
    "youtube.com/watch?v=GxggU7XoCLg",    # 无 scheme → 按路径（ADR-043 D2 显式边界）
    "GxggU7XoCLg",                        # 裸 id（parse_video_id 能解，但形态是路径）
    "",
    None,
])
def test_classify_input_path(raw):
    assert classify_input(raw) == INPUT_PATH


def test_looks_like_url_requires_scheme():
    """URL 谓词只看语法（带 scheme）—— 供路径卫生校验的 URL 豁免复用（ADR-043 D8）。"""
    assert looks_like_url("https://a/b")
    assert looks_like_url("http://a/b")
    assert looks_like_url("HTTPS://a/b")
    assert not looks_like_url("C:/videos/a.mp4")
    assert not looks_like_url(r"C:\videos\a.mp4")
    assert not looks_like_url(r"C://videos/a.mp4")
    assert not looks_like_url(r"\\server\share\a.mp4")
    assert not looks_like_url("youtube.com/watch?v=x")
    assert not looks_like_url("")
    assert not looks_like_url(None)


def test_classify_input_is_total():
    """判定对任何输入都必须给出三态之一（分发层据此互斥分支，不得抛异常）。"""
    for raw in ("", None, "   ", "?", "://", "http://", "https://例え.bd/x"):
        assert classify_input(raw) in (INPUT_YOUTUBE, INPUT_URL_UNSUPPORTED,
                                       INPUT_PATH)
