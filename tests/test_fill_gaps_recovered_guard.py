"""ADR-020 补遗：fill_gaps 恢复段（_recovered）的幻觉守卫。

背景：jimmy 视频实战暴露 fill_gaps 的补洞恢复段绕过了 merge.py 的
``drop_hallucination_segments``（它只在 fill_gaps 之前作用于 Whisper 原产段）。
恢复段仅经文本相似度 ``_is_echo`` 过滤，不看时间戳几何，导致 7 条
"骑在已确认音频上" 的脑补回音进入最终字幕（Don't worry. / I'm fucking
fired! / Субтитры... / I'm a clown. / Hi, son. / Now what? / This is bad.）。

本测试用 jimmy 的实测数据固化守卫，并验证真实恢复段（Got it walking. 等）
不被误杀、collapse 路径不受重叠信号 A 影响。

信号（任一命中即判幻觉，全保守防误杀真实补洞语音）：
  A. 与任一现有段窗口重叠 > overlap_eps（默认 0.12s）-> 骑在已确认音频上
  B. 词数 >= min_words 且 语速(wps) > max_wps（默认 8.0）-> 物理不可能
  C. avg_logprob < thr 且 no_speech_prob >= no_speech_thr -> Whisper 低自信
  其中信号 A 在 collapse 替换路径关闭（被替换段本就与邻居重叠）。
"""
import sys

from video_translate import fill_gaps as F


def _w(*pairs):
    """Helper: build a words list from (word, start, end) triples."""
    return [{"word": w, "start": s, "end": e} for (w, s, e) in pairs]


# ---------------------------------------------------------------------------
# 单元测试：_is_recovered_hallucination 直接调用（确定性、可复现）
# ---------------------------------------------------------------------------

def test_signal_a_overlap_drops_dont_worry():
    """jimmy 13.40-13.56 'Don't worry.' 整体嵌在 index3 [12.38,13.60] 内，
    与现有段重叠 0.16s > 0.12s -> 丢弃。"""
    cand = {"start": 13.40, "end": 13.56, "text": "Don't worry.",
            "words": _w(("Don't", 13.40, 13.52), ("worry.", 13.52, 13.56))}
    seg3 = {"start": 12.38, "end": 13.60, "text": "going to work."}
    assert F._is_recovered_hallucination(cand, [seg3]) is True


def test_signal_a_overlap_drops_hi_son():
    """jimmy 254.38-254.66 'Hi, son.' 与 'Awesome.' [254.06,254.58] 重叠 0.2s -> 丢弃。"""
    cand = {"start": 254.38, "end": 254.66, "text": "Hi, son.",
            "words": _w(("Hi,", 254.38, 254.62), ("son.", 254.66, 254.66))}
    awesome = {"start": 254.06, "end": 254.58, "text": "Awesome."}
    assert F._is_recovered_hallucination(cand, [awesome]) is True


def test_signal_a_overlap_drops_now_what():
    """jimmy 283.36-284.12 'Now what?' 侵入前段尾部 0.2s（>0.12）-> 丢弃。
    注意：原方案用"重叠占自身比例>40%"只会得到 26%，漏掉它；绝对重叠量正确拦截。"""
    cand = {"start": 283.36, "end": 284.12, "text": "Now what?",
            "words": _w(("Now", 283.36, 283.76), ("what?", 283.76, 284.12))}
    prev = {"start": 280.0, "end": 283.56, "text": "We got a game, folks."}
    assert F._is_recovered_hallucination(cand, [prev]) is True


def test_signal_b_high_wps_drops_im_a_clown():
    """jimmy 193.49-193.67 'I'm a clown.' 3 词/0.18s = 16.7 wps > 8 -> 丢弃。"""
    cand = {"start": 193.49, "end": 193.67, "text": "I'm a clown.",
            "words": _w(("I'm", 193.49, 193.49), ("a", 193.49, 193.49),
                        ("clown.", 193.49, 193.67))}
    seg54 = {"start": 191.61, "end": 193.69, "text": "that I'm a clown"}
    assert F._is_recovered_hallucination(cand, [seg54]) is True


def test_signal_c_low_confidence_drops_phantom():
    """携带 low avg_logprob + 高 no_speech_prob 的恢复段 -> 丢弃（第五信号）。"""
    cand = {"start": 111.54, "end": 113.04, "text": "Thank you.",
            "words": _w(("Thank", 111.54, 112.94), ("you.", 112.94, 113.04)),
            "avg_logprob": -2.1, "no_speech_prob": 0.75}
    # 此段孤立在洞中央（无重叠），信号 A/B 不命中，必须靠信号 C
    assert F._is_recovered_hallucination(cand, []) is True


def test_real_recovery_not_dropped_isolated():
    """jimmy 'Got it walking.' [35.3,36.72] 孤立在洞中央、语速正常 -> 保留。"""
    cand = {"start": 35.3, "end": 36.72, "text": "Got it walking.",
            "words": _w(("Got", 35.3, 35.7), ("it", 35.7, 36.2),
                        ("walking.", 36.2, 36.72))}
    assert F._is_recovered_hallucination(cand, []) is False


def test_real_recovery_not_dropped_singing():
    """jimmy 'And the vocals up...' [17.0,18.7] 孤立、语速 5.9 wps < 8 -> 保留
    （这是唱歌听错，属第二/三类，本轮不处理，守卫不动它）。"""
    cand = {"start": 17.0, "end": 18.7, "text": "And the vocals up a little bit.",
            "words": _w(("And", 17.0, 17.2), ("the", 17.2, 17.5),
                        ("vocals", 17.5, 18.0), ("up", 18.0, 18.3),
                        ("a", 18.3, 18.5), ("little", 18.5, 18.6),
                        ("bit.", 18.6, 18.7))}
    assert F._is_recovered_hallucination(cand, []) is False


def test_long_recovered_not_dropped_by_boundary_overlap():
    """jimmy 'anxious. There's a difference.' [362.58,364.46] 与前段
    '...you get anxi[ous]' [360.74,362.78] 边界模糊重叠 0.20s，但它是 10 词长句
    （真实语音续接），信号 A 对长段豁免 -> 保留。"""
    cand = {"start": 362.58, "end": 364.46,
            "text": "anxious. There's a difference.",
            "words": _w(("anxious.", 362.58, 362.9), ("There's", 362.9, 363.2),
                        ("a", 363.2, 363.3), ("difference.", 363.3, 364.46))}
    prior = {"start": 360.74, "end": 362.78, "text": "Were you nervous doing that you get anxi"}
    assert F._is_recovered_hallucination(cand, [prior]) is False


def test_collapse_path_disables_overlap_signal():
    """collapse 替换路径必须关闭信号 A：被替换段本身与邻居重叠是预期的，
    否则会误杀真正替换回来的语音。"""
    cand = {"start": 55.87, "end": 56.61, "text": "Real recovered line.",
            "words": _w(("Real", 55.87, 56.0), ("recovered", 56.0, 56.3),
                        ("line.", 56.3, 56.61))}
    # 与邻居重叠（模拟被替换段窗口），但 collapse 路径只查 B/C
    neighbor = {"start": 55.27, "end": 56.37, "text": "motherfucking"}
    assert F._is_recovered_hallucination(cand, [neighbor], check_overlap=False) is False


# ---------------------------------------------------------------------------
# 端到端：确认 _decode_once 真正调用守卫 + 携带置信度字段
# ---------------------------------------------------------------------------

def test_end_to_end_hole_guard_drops_overlapping_recovery(monkeypatch):
    """端到端：hole [1,5] 解码恢复出与现有段重叠的脑补 -> 应被守卫丢弃。"""
    import video_translate.fill_gaps as FG
    import sys as _sys

    monkeypatch.setattr(FG, "probe_duration", lambda p: 6.0)
    monkeypatch.setattr(FG, "extract_chunk", lambda *a, **k: None)
    monkeypatch.setattr(FG, "resolve_device", lambda *a, **k: ("cpu", "int8"))

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, **kw):
            # 恢复段 [0.9, 1.1] 与现有段 seg0 [0.0,1.0] 重叠 0.1s -> 命中 A
            S = type("S", (), {})
            s = S()
            s.text = "Don't worry."
            s.start = 0.9
            s.end = 1.1
            s.words = [type("W", (), {"word": "Don't", "start": 0.9, "end": 1.0})(),
                       type("W", (), {"word": "worry.", "start": 1.0, "end": 1.1})()]
            s.avg_logprob = -1.5
            s.no_speech_prob = 0.7
            s.compression_ratio = 1.0
            return [s], None

    fake = type(_sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    _sys.modules["faster_whisper"] = fake

    segments = [
        {"start": 0.0, "end": 1.0, "text": "hello world",
         "words": [{"word": "hello", "start": 0.0, "end": 0.5},
                   {"word": "world", "start": 0.5, "end": 1.0}]},
        {"start": 5.0, "end": 6.0, "text": "goodbye now",
         "words": [{"word": "goodbye", "start": 5.0, "end": 5.5},
                   {"word": "now", "start": 5.5, "end": 6.0}]},
    ]
    out = FG.fill_gaps("vid.mp4", segments, silence_intervals=[],
                       progress=lambda *_: None)
    # 脑补 'Don't worry.' 被守卫丢弃，初始两段原样保留
    assert len(out) == 2
    assert {s["text"] for s in out} == {"hello world", "goodbye now"}


def test_end_to_end_hole_guard_keeps_isolated_recovery(monkeypatch):
    """端到端：hole [1,5] 解码恢复出孤立在洞中央的真实语音 -> 保留。"""
    import video_translate.fill_gaps as FG
    import sys as _sys

    monkeypatch.setattr(FG, "probe_duration", lambda p: 6.0)
    monkeypatch.setattr(FG, "extract_chunk", lambda *a, **k: None)
    monkeypatch.setattr(FG, "resolve_device", lambda *a, **k: ("cpu", "int8"))

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, **kw):
            S = type("S", (), {})
            s = S()
            s.text = "Got it walking."  # 孤立 [2.5,4.0]，无重叠
            s.start = 2.5
            s.end = 4.0
            s.words = [type("W", (), {"word": "Got", "start": 2.5, "end": 2.9})(),
                       type("W", (), {"word": "it", "start": 2.9, "end": 3.4})(),
                       type("W", (), {"word": "walking.", "start": 3.4, "end": 4.0})()]
            s.avg_logprob = -0.4
            s.no_speech_prob = 0.1
            s.compression_ratio = 1.0
            return [s], None

    fake = type(_sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    _sys.modules["faster_whisper"] = fake

    segments = [
        {"start": 0.0, "end": 1.0, "text": "hello world",
         "words": [{"word": "hello", "start": 0.0, "end": 0.5},
                   {"word": "world", "start": 0.5, "end": 1.0}]},
        {"start": 5.0, "end": 6.0, "text": "goodbye now",
         "words": [{"word": "goodbye", "start": 5.0, "end": 5.5},
                   {"word": "now", "start": 5.5, "end": 6.0}]},
    ]
    out = FG.fill_gaps("vid.mp4", segments, silence_intervals=[],
                       progress=lambda *_: None)
    assert len(out) == 3
    assert any(s["text"] == "Got it walking." for s in out)
    # 携带的置信度字段应贯通到最终段
    got = [s for s in out if s["text"] == "Got it walking."][0]
    assert got["avg_logprob"] == -0.4


def test_end_to_end_carries_confidence_fields(monkeypatch):
    """恢复段须携带 avg_logprob/no_speech_prob（信号 C 的前提，向后兼容）。"""
    import video_translate.fill_gaps as FG
    import sys as _sys

    monkeypatch.setattr(FG, "probe_duration", lambda p: 6.0)
    monkeypatch.setattr(FG, "extract_chunk", lambda *a, **k: None)
    monkeypatch.setattr(FG, "resolve_device", lambda *a, **k: ("cpu", "int8"))

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav, **kw):
            S = type("S", (), {})
            s = S()
            s.text = "isolated real."  # 孤立、高置信
            s.start = 2.5
            s.end = 4.0
            s.words = [type("W", (), {"word": "isolated", "start": 2.5, "end": 3.0})(),
                       type("W", (), {"word": "real.", "start": 3.0, "end": 4.0})()]
            s.avg_logprob = -0.5
            s.no_speech_prob = 0.2
            s.compression_ratio = 1.1
            return [s], None

    fake = type(_sys)("faster_whisper")
    fake.WhisperModel = FakeModel
    _sys.modules["faster_whisper"] = fake

    segments = [
        {"start": 0.0, "end": 1.0, "text": "hello world",
         "words": [{"word": "hello", "start": 0.0, "end": 0.5},
                   {"word": "world", "start": 0.5, "end": 1.0}]},
        {"start": 5.0, "end": 6.0, "text": "goodbye now",
         "words": [{"word": "goodbye", "start": 5.0, "end": 5.5},
                   {"word": "now", "start": 5.5, "end": 6.0}]},
    ]
    out = FG.fill_gaps("vid.mp4", segments, silence_intervals=[],
                       progress=lambda *_: None)
    iso = [s for s in out if s["text"] == "isolated real."][0]
    assert iso["avg_logprob"] == -0.5
    assert iso["no_speech_prob"] == 0.2
    assert iso["_recovered"] is True
