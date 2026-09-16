"""ADR-040 — 字幕文本单行不变量回归测试。

事故来源（用户报告）：**单语 `zh.srt` / `en.srt` 里也有回车换行**。
单语字幕本应"一条字幕一行"，根因是文本内容里的 `\\n` 从未被清洗，
从源头（ASR / 译文 / Agent 翻译产物）一路透传到 SRT。

本测试锁定：
  1. `to_single_line` 纯函数行为（各换行形态）；
  2. `generate.build_outputs` 的**边界清洗**（zh / en 含 `\\n` 时必须单行化）；
  3. `transcribe._seg_to_dict` 的**源头清洗**；
  4. `translate.translate_segments` 的**译文清洗**；
  5. **不可误伤**：`bilingual.srt` 的中英分行（Spec 04 设计）必须保留。
"""
from __future__ import annotations

import json

import pytest

from video_translate.generate import build_outputs
from video_translate.text_utils import to_single_line


# --------------------------- 1. to_single_line 纯函数 ---------------------------

@pytest.mark.parametrize("raw,expected", [
    ("a\nb", "a b"),
    ("a\r\nb", "a b"),          # CRLF
    ("a\rb", "a b"),            # 裸 CR
    ("a\n\nb", "a b"),          # 连续换行 → 单空格
    ("a \n b", "a b"),          # 换行两侧空格被吸收
    ("\n a \n", "a"),           # 首尾换行 + 空白
    ("", ""),
    (None, ""),
    ("no break", "no break"),
    ("a\tb", "a\tb"),           # 制表符不属于换行，保持不动
    ("你好\n世界", "你好 世界"),   # 中文同样适用
])
def test_to_single_line(raw, expected):
    assert to_single_line(raw) == expected


def test_to_single_line_is_idempotent():
    once = to_single_line("a\n\n b \n c")
    assert to_single_line(once) == once


# --------------------------- 2. generate 边界清洗（核心事故） ---------------------------

def _cue_text_lines(srt: str) -> list[str]:
    """抽出 SRT 的文本行（跳过序号行与时间行）。"""
    out: list[str] = []
    for block in srt.strip().split("\n\n"):
        lines = block.split("\n")
        out.extend(lines[2:])           # 前两行是 index / timestamp
    return out


def test_zh_srt_flattens_embedded_newline():
    """事故几何：译文本身带换行 → zh.srt 不得出现额外行。"""
    segs = [{"start": 0.0, "end": 1.0, "text": "hello world"}]
    zh = {0: "你好\n世界"}
    out = build_outputs(segs, zh)
    lines = out[".zh.srt"].strip().split("\n")
    assert lines[2] == "你好 世界"
    assert len(lines) == 3, f"单语 cue 应恰好 3 行（index/time/text），实得 {lines!r}"


def test_en_srt_flattens_embedded_newline():
    segs = [{"start": 0.0, "end": 1.0, "text": "hello\nworld"}]
    out = build_outputs(segs, {})
    lines = out[".en.srt"].strip().split("\n")
    assert lines[2] == "hello world"
    assert len(lines) == 3


def test_zh_srt_multiple_embedded_newlines():
    segs = [{"start": 0.0, "end": 1.0, "text": "x"}]
    zh = {0: "第一句\n\n第二句\n第三句"}
    out = build_outputs(segs, zh)
    lines = out[".zh.srt"].strip().split("\n")
    assert len(lines) == 3
    assert "\n" not in lines[2]


def test_txt_output_flattens_embedded_newline():
    """`.txt` 脚本同样不该被译文换行撑破结构。"""
    segs = [{"start": 0.0, "end": 1.0, "text": "hello\nworld"}]
    zh = {0: "你好\n世界"}
    out = build_outputs(segs, zh)
    # txt 有固定结构行（[t0 -> t1] / 中文: / 英文:），正文不得再含换行
    assert "中文: 你好 世界" in out[".txt"]
    assert "英文: hello world" in out[".txt"]


# --------------------------- 5. 不误伤：中英分行是设计 ---------------------------

def test_bilingual_keeps_design_two_lines():
    """Spec 04 设计：bilingual.srt 每条 cue = 中文行 + 英文行（2 行）。"""
    segs = [{"start": 0.0, "end": 1.0, "text": "hello"}]
    zh = {0: "你好"}
    out = build_outputs(segs, zh)
    texts = _cue_text_lines(out[".bilingual.srt"])
    assert texts == ["你好", "hello"]


def test_bilingual_content_newline_still_flattened():
    """双语保留中英分行，但文本内容里的换行照样要被压平。"""
    segs = [{"start": 0.0, "end": 1.0, "text": "hello\nworld"}]
    zh = {0: "你好\n世界"}
    out = build_outputs(segs, zh)
    texts = _cue_text_lines(out[".bilingual.srt"])
    assert texts == ["你好 世界", "hello world"]


# --------------------------- 3. transcribe 源头清洗 ---------------------------

class _FakeWhisperSeg:
    def __init__(self, text: str):
        self.start = 0.0
        self.end = 1.0
        self.text = text
        self.words = []
        self.avg_logprob = -0.3
        self.no_speech_prob = 0.1
        self.compression_ratio = 1.4


def test_seg_to_dict_flattens_newline():
    from video_translate.transcribe import _seg_to_dict

    d = _seg_to_dict(_FakeWhisperSeg("hello\nworld"), 0.0)
    assert d["text"] == "hello world"


# --------------------------- 4. translate 译文清洗 ---------------------------

def test_translate_flattens_engine_output(tmp_path):
    from video_translate.translate import translate_segments

    segs = [{"start": 0.0, "end": 1.0, "text": "hello"}]
    segs_path = tmp_path / "demo.segments_en.json"
    segs_path.write_text(json.dumps(segs), encoding="utf-8")
    out_path = tmp_path / "demo.zh_segments.json"

    res = translate_segments(
        str(segs_path), str(out_path),
        translate_fn=lambda _t: "你好\n世界",
        progress=lambda *_a, **_k: None,
    )
    assert res["0"] == "你好 世界"
    # 落盘内容也必须干净
    assert json.loads(out_path.read_text(encoding="utf-8"))["0"] == "你好 世界"
