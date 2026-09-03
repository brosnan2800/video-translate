"""ADR-036: fill_gaps recovery 起点 pad 回溯 —— hard-cut prefix collapse 修复。

回归场景：洞起点（gs）落在前一句半句处（主转写把前句尾巴切进了上一句，
洞从半句中间开始）。旧实现 pad 仅 ±0.5s，解码窗口起点仍在前句内部 ->
Whisper prefix collapse，只掏出洞尾碎片，洞内对白丢失。

修复后：_probe 尝试大 pad（2/4/6s）把解码起点推到前句开头，并用洞内
coverage 打分选取能真正填洞的 pad；前句尾巴由既有 _is_echo 守卫拦截。
"""
import sys

from video_translate import fill_gaps as F


# ---------------------------------------------------------------------------
# 单元测试：模块级纯函数（确定、可复现）
# ---------------------------------------------------------------------------

def test_coverage_in_hole_only_counts_hole_interval():
    """大 pad 拉回的前句尾巴（洞外部分）不得计入 coverage，否则会虚高早停。"""
    cand = [
        {"start": 1.0, "end": 4.0},   # 完全在洞外（前句尾巴）
        {"start": 9.0, "end": 12.0},  # 横跨洞尾：只算 10..12 = 2s
        {"start": 13.0, "end": 16.0}, # 完全在洞内 = 3s
    ]
    assert F._coverage_in_hole(cand, 10.0, 16.0) == 5.0


def test_coverage_in_hole_empty_when_all_outside():
    cand = [{"start": 0.0, "end": 4.0}, {"start": 16.0, "end": 20.0}]
    assert F._coverage_in_hole(cand, 10.0, 16.0) == 0.0


def test_probe_pads_short_hole_single_pad():
    """短洞只试小 pad，避免过度回溯拖入邻居句。"""
    assert F._probe_pads_for_window(2.0) == F._PROBE_PADS[:1]
    assert F._probe_pads_for_window(3.99) == F._PROBE_PADS[:1]


def test_probe_pads_wide_hole_all_pads():
    assert F._probe_pads_for_window(4.0) == F._PROBE_PADS
    assert F._probe_pads_for_window(11.3) == F._PROBE_PADS


# ---------------------------------------------------------------------------
# 集成测试：mock Whisper，模拟"起点决定 prefix collapse"
# ---------------------------------------------------------------------------

def _mk_word(word, s, e):
    return type("W", (), {"word": word, "start": s, "end": e})


def _mk_seg(text, s, e, words):
    return type("Seg", (), {
        "text": text, "start": s, "end": e, "words": words,
        "avg_logprob": -0.5, "no_speech_prob": 0.1, "compression_ratio": 1.0,
    })


def test_hardcut_recovery_pulls_back_to_sentence_start(monkeypatch):
    """洞起点落在前句半句时，大 pad 必须恢复洞内对白，前句回声被拦截。"""
    captured_ss = {"v": None}

    def fake_extract_chunk(src, wav, ss, dur):
        captured_ss["v"] = ss

    monkeypatch.setattr(F, "extract_chunk", fake_extract_chunk)
    monkeypatch.setattr(F, "probe_duration", lambda p: 25.0)
    monkeypatch.setattr(F, "resolve_device", lambda *a, **k: ("cpu", "int8"))

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, **kw):
            ss = captured_ss["v"]
            if ss >= 4.5:
                # 解码起点仍在半句上 -> prefix collapse，只给窗首碎片（洞内覆盖 0）
                return [_mk_seg("first half of sentence", 0.0, 0.2,
                                [_mk_word("first", 0.0, 0.2)])], None
            # 起点退到前句开头之前 -> 完整解码：绝对 (0,5) 前句 + 绝对 (5,15) 洞内对白
            return [
                _mk_seg("first half of sentence spoken before the cut",
                        0.0, 5.0 - ss, [_mk_word("first", 0.0, 5.0 - ss)]),
                _mk_seg("recovered dialogue inside the gap",
                        5.0 - ss, 15.0 - ss,
                        [_mk_word("recovered", 5.0 - ss, 15.0 - ss)]),
            ], None

    fake = type(sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)

    segments = [
        {"start": 0.0, "end": 5.0,
         "text": "first half of sentence spoken before the cut"},
        {"start": 15.0, "end": 25.0,
         "text": "after the gap there is more dialogue"},
    ]
    out = F.fill_gaps("vid.mp4", segments, silence_intervals=[],
                      progress=lambda *_: None)

    recovered = [s for s in out if s.get("_recovered")]
    # 洞内对白必须被恢复（核心回归断言）
    assert any(5.0 <= float(s["start"]) < 15.0
               and "recovered dialogue" in s["text"] for s in recovered), recovered
    # 前句尾巴不应作为回声被重复插入（_is_echo / 信号 A 拦截）
    assert not any(s.get("_recovered")
                   and s["text"].strip() == "first half of sentence"
                   for s in out), out
