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
  C. no_speech_prob >= no_speech_thr（默认 0.6）-> Whisper 自判该窗口主要
     非语音（恢复段最强信号，avg_logprob 不设闸：'We'll be right back.'
     0.766 / 'Thank you.' 0.799 即便 avg>-1.0 也拦；C2 avg_logprob < thr
     兜旧缓存缺 no_speech_prob）
  D. 零时长词（start >= end）>= min_zero_dur_words（默认 2）-> DTW 坍缩指纹
     （ADR-020 信号 4 应用于恢复段；kathy_meta_vlog 开头 10 秒非语音能量被
     硬拼成 'Hubsan x4 H502E Desire 2-3-18'，靠 3 个零时长尾词命中 D）
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


def test_signal_d_zero_dur_words_drops_hubsan_model_text():
    """kathy_meta_vlog 0-10.7s 恢复段 'Hubsan x4 H502E Desire 2-3-18'（真实实测）：
    对开头非语音能量硬拼的幻觉。无重叠、0.66 wps、avg_logprob -0.942
    （>-1.0 阈值）全部逃过 A/B/C，但尾 3 词 "2"/"-3"/"-18" 零时长 -> 命中 D。
    """
    cand = {"start": 0.0, "end": 10.66, "text": "Hubsan x4 H502E Desire 2-3-18",
            "words": _w((" Hubsan", 0.0, 2.38), (" x4", 2.38, 4.78),
                        (" H502E", 4.78, 9.6), (" Desire", 9.6, 10.66),
                        (" 2", 10.66, 10.66), (" -3", 10.66, 10.66),
                        (" -18", 10.66, 10.66)),
            "avg_logprob": -0.942, "no_speech_prob": 0.642}
    # 孤立在开头（无邻居可重叠），证明信号 D 独立生效
    assert F._is_recovered_hallucination(cand, []) is True


def test_signal_c_high_no_speech_drops_we_ll_be_right_back():
    """kathy_meta_vlog 'We'll be right back.'（no_speech=0.766, avg=-0.650）：
    词数恰好=4（信号 A 的 <4 条件躲过）、无零时长词（信号 D 不命中）、
    avg_logprob>-1.0（旧信号 C 的 AND 漏过）——但 no_speech=0.766>=0.6，
    强化后的信号 C 单独拦截。独立重转写该窗口只得到 'Taylor Street.'
    （前段尾部），确证幻觉。
    """
    cand = {"start": 17.51, "end": 19.21, "text": "We'll be right back.",
            "words": _w((" We'll", 17.51, 18.09), (" be", 18.09, 18.11),
                        (" right", 18.11, 18.13), (" back.", 18.13, 19.21)),
            "avg_logprob": -0.650, "no_speech_prob": 0.766}
    assert F._is_recovered_hallucination(cand, []) is True


def test_signal_c_does_not_drop_low_no_speech_recovery():
    """低 no_speech_prob 的真实恢复段不受强化信号 C 影响：jimmy 'Got it
    walking.' no_speech=0.1 -> 保留。
    """
    cand = {"start": 35.3, "end": 36.72, "text": "Got it walking.",
            "words": _w(("Got", 35.3, 35.7), ("it", 35.7, 36.2),
                        ("walking.", 36.2, 36.72)),
            "avg_logprob": -0.4, "no_speech_prob": 0.1}
    assert F._is_recovered_hallucination(cand, []) is False


def test_signal_d_single_zero_dur_word_alone_is_not_enough():
    """单个零时长词不判幻觉（对齐边缘抖动常见）——阈值 min_zero_dur_words=2。"""
    cand = {"start": 10.0, "end": 12.0, "text": "one collapsed word here",
            "words": _w(("one", 10.0, 10.4), ("collapsed", 10.5, 10.5),
                        ("word", 10.6, 11.0), ("here", 11.1, 12.0))}
    assert F._is_recovered_hallucination(cand, []) is False


# ---------------------------------------------------------------------------
# ADR-036 F3：信号 C 按段长分级 —— 长段真实语音不得再被高 nsp 误杀
#
# 事故来源：videos/Nobody Can Handle Christopher Walken's STRANGE Hum.mp4
# 13:41–13:52（洞 821.44–832.74 无字幕）。fill_gaps 长洞首子窗 pad=4.0 解码
# [811.44, 837.44] 时，whisper 在 no_speech_threshold=0.0 强制解码下吐出了三段
# 正确对白，却同时自报 no_speech_prob=0.892；旧信号 C 一刀切（nsp>=0.6 即判
# 幻觉）把三段全部丢弃，首子窗 coverage=0，洞头永久留空。
#
# 下列几何数据为实测采集：零时长词=0（D 未触发）、avg_logprob=-0.2345 > -1.0
# （C2 未触发）、语速正常且与邻居无重叠（A/B 未触发）—— 唯一杀手就是信号 C。
# ---------------------------------------------------------------------------

_ACC_NSP = 0.89208984375
_ACC_ALP = -0.2345145121216774


def test_adr036_head_segment_21w_high_nsp_not_dropped():
    """事故段 1：21 词、nsp=0.892、孤立无重叠 —— 真实对白，必须保留（旧实现误杀）。"""
    cand = {"start": 821.44, "end": 826.76,
            "text": "There's one guy who could do and I think we all would "
                    "watch that guy is Chris Walken. Oh god. Yes",
            "words": _w((" There's", 821.44, 821.96), (" one", 821.96, 822.06),
                        (" guy", 822.06, 822.36), (" who", 822.36, 822.56),
                        (" could", 822.56, 822.68), (" do", 822.68, 822.84),
                        (" and", 822.84, 823.12), (" I", 823.12, 823.36),
                        (" think", 823.36, 823.5), (" we", 823.5, 823.58),
                        (" all", 823.58, 823.76), (" would", 823.76, 823.88),
                        (" watch", 823.88, 824.1), (" that", 824.1, 824.46),
                        (" guy", 824.46, 824.72), (" is", 824.72, 825.26),
                        (" Chris", 825.26, 825.46), (" Walken.", 825.46, 825.98),
                        (" Oh", 826.0, 826.16), (" god.", 826.16, 826.42),
                        (" Yes", 826.48, 826.76)),
            "no_speech_prob": _ACC_NSP, "avg_logprob": _ACC_ALP}
    assert F._is_recovered_hallucination(cand, []) is False


def test_adr036_head_segment_7w_high_nsp_not_dropped():
    """事故段 2：7 词（刚过 6 词短段阈值）—— 必须保留。"""
    cand = {"start": 827.3, "end": 829.28,
            "text": "Chris will be up there going I'm",
            "words": _w((" Chris", 827.3, 827.82), (" will", 827.82, 828.04),
                        (" be", 828.04, 828.12), (" up", 828.12, 828.24),
                        (" there", 828.24, 828.38), (" going", 828.38, 828.52),
                        (" I'm", 828.52, 829.28)),
            "no_speech_prob": _ACC_NSP, "avg_logprob": _ACC_ALP}
    assert F._is_recovered_hallucination(cand, []) is False


def test_adr036_head_segment_16w_high_nsp_not_dropped():
    """事故段 3：16 词 —— 必须保留。"""
    cand = {"start": 830.22, "end": 837.06,
            "text": "Inside you so deep inside you now and you now inside "
                    "you deep inside you now",
            "words": _w((" Inside", 830.22, 830.74), (" you", 830.74, 831.12),
                        (" so", 831.12, 832.18), (" deep", 832.18, 832.48),
                        (" inside", 832.48, 832.94), (" you", 832.94, 833.4),
                        (" now", 833.4, 833.9), (" and", 833.9, 834.52),
                        (" you", 834.52, 834.74), (" now", 834.74, 835.08),
                        (" inside", 835.08, 835.5), (" you", 835.5, 835.9),
                        (" deep", 835.9, 836.14), (" inside", 836.14, 836.5),
                        (" you", 836.5, 836.8), (" now", 836.8, 837.06)),
            "no_speech_prob": _ACC_NSP, "avg_logprob": _ACC_ALP}
    assert F._is_recovered_hallucination(cand, []) is False


def test_adr036_boundary_5w_high_nsp_still_dropped():
    """分级不得放宽短段：5 词 < 6 且 nsp 高 —— 仍判幻觉（守住阈值下界）。"""
    cand = {"start": 50.0, "end": 51.8, "text": "Thanks for watching this video.",
            "words": _w((" Thanks", 50.0, 50.4), (" for", 50.4, 50.7),
                        (" watching", 50.7, 51.0), (" this", 51.0, 51.3),
                        (" video.", 51.3, 51.8)),
            "no_speech_prob": 0.88, "avg_logprob": -0.5}
    assert F._is_recovered_hallucination(cand, []) is True


def test_adr036_boundary_6w_high_nsp_not_dropped():
    """阈值上界：6 词（>= 6）+ nsp 高 —— 长段豁免生效，保留。"""
    cand = {"start": 60.0, "end": 62.0,
            "text": "Thanks for watching this whole video clip.",
            "words": _w((" Thanks", 60.0, 60.3), (" for", 60.3, 60.6),
                        (" watching", 60.6, 60.9), (" this", 60.9, 61.2),
                        (" whole", 61.2, 61.5), (" clip.", 61.5, 62.0)),
            "no_speech_prob": 0.88, "avg_logprob": -0.5}
    assert F._is_recovered_hallucination(cand, []) is False


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
