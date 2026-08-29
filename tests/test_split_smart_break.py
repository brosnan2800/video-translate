"""ADR-022: smart break points & leading-orphan rejoin.

jimmy.mp4 实测 48 处「句尾词被掐到下一条字幕」，归因：
  - 39 处 `_split_by_length` 贪心切分（装满 42 字符就在当前词切，不看语义：
    "we don't | know"、"sister | deidre"、"Annalise and | ..."）
  -  8 处 `_split_by_gap` 真实停顿（>1s，Spec 13 要求保留）——但其句首
     碎片（`because`、`But I`）是不完整句，V4 左并因 dur 卡线漏接
  -  1 处词时间戳塌缩（merge 条件4，本轮不动）

修复（剪映 42 字符上限不动，只挪切点）：
  A. `_split_by_length` 智能切点：贪心触发时在窗口内回退找
     (1) 最靠后的标点边界（`months,` 后切，不切 `don't|know`）
     (2) 无标点时切在 >0.3s 的最大词间停顿（说话人气口）
     (3) 都没有 → 贪心兜底（旧行为）
     约束：前组 >=2 词；尾组须能容纳触发词（永不超 max_chars）。
  B. `rejoin_leading_orphans`：句首连接词孤儿（<=2 词、无 [.!?] 结尾、
     距右段 <1.5s）并回右段（`because` + `you do take`）。真实停顿在
     cue 内部存活（时间窗取并集，不重算时间戳，ADR-012 不变量）。
"""
from video_translate.merge import (
    _split_by_length,
    rejoin_leading_orphans,
    split_long_cues,
)


def _w(words, gap=0.1, gaps=None, dur=0.4):
    """Build a word list with sequential timestamps; `gaps[i]` = extra silence
    before word i (i > 0)."""
    out = []
    t = 0.0
    for i, wd in enumerate(words):
        if i > 0:
            t += (gaps or {}).get(i, gap)
        out.append({"word": wd, "start": round(t, 3),
                    "end": round(t + dur, 3)})
        t += dur
    return out


def _txt(group):
    return " ".join(w["word"] for w in group)


def _wsum(group):
    return sum(len(w["word"].strip()) for w in group)


# ---------------------------------------------------------------------------
# A. 智能切点
# ---------------------------------------------------------------------------

def test_break_prefers_punctuation():
    """jimmy #2 实测：'So we're at home for three months, we don't know if
    these jokes are going to work.' 贪心切在 'if' 后（词和 41），把
    'don't | know' 短语劈开前兆；智能切点应回退到逗号 'months,' 后。"""
    words = _w(["so", "we're", "at", "home", "for", "three", "months,",
                "we", "don't", "know", "if", "these", "jokes", "are",
                "going", "to", "work."])
    groups = _split_by_length(words, max_chars=42)
    assert len(groups) == 2
    assert _txt(groups[0]).endswith("months,")
    assert _txt(groups[0]) == "so we're at home for three months,"
    assert _txt(groups[1]) == "we don't know if these jokes are going to work."
    assert all(_wsum(g) <= 42 for g in groups)


def test_break_prefers_largest_pause():
    """无标点时切在最大词间停顿（气口）：w3|w4 间 0.8s，其余 0.1s。
    w1..w5 各 8 字符装满 40，w6 触发贪心切分；智能切点应在气口 w4 前。"""
    words = _w(["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd",
                "eeeeeeee", "ffffffff", "gggggggg"],
               gaps={3: 0.8})  # 0.8s pause before word index 3 (dddddddd)
    groups = _split_by_length(words, max_chars=40)
    assert len(groups) == 2
    assert _txt(groups[0]) == "aaaaaaaa bbbbbbbb cccccccc"
    assert _txt(groups[1]) == "dddddddd eeeeeeee ffffffff gggggggg"
    assert all(_wsum(g) <= 40 for g in groups)


def test_break_falls_back_to_greedy():
    """无标点、均匀停顿（0.1s < 0.3s 阈值）→ 贪心兜底，与旧行为一致。"""
    words = _w("the quick brown fox jumps over the lazy dog and then some more words here".split())
    groups = _split_by_length(words, max_chars=10)
    # greedy expectation (word-char sum, no spaces, <= 10)
    assert [_txt(g) for g in groups] == [
        "the quick", "brown fox", "jumps over", "the lazy dog",
        "and then", "some more", "words here",
    ]
    assert all(_wsum(g) <= 10 for g in groups)


def test_break_never_exceeds_cap():
    """不变量：任意输入，每组词和 <= max_chars 且词序保全。"""
    import random
    rng = random.Random(42)
    words = _w(["".join(rng.choice("abcdefghij") for _ in range(rng.randint(1, 12)))
                for _ in range(200)], gap=0.05)
    orig = [w["word"] for w in words]
    groups = _split_by_length(words, max_chars=42)
    assert all(_wsum(g) <= 42 for g in groups)
    assert [w["word"] for g in groups for w in g] == orig


def test_break_min_front_two_words():
    """标点太靠前（第 1 词 'well,'）不切出 1 词前组 → 放弃该标点，
    无停顿信号 → 贪心兜底。"""
    words = _w(["well,", "aaaa", "bbbb", "cccc", "dddd", "eeee", "ffff"])
    groups = _split_by_length(words, max_chars=28)
    # greedy: well,+aaaa+...+eeee = 25, ffff would make 29 > 28
    assert [_txt(g) for g in groups] == [
        "well, aaaa bbbb cccc dddd eeee", "ffff",
    ]


# ---------------------------------------------------------------------------
# B. 句首孤儿并右（because / But I 类）
# ---------------------------------------------------------------------------

def _seg(text, start, end, words=None):
    return {"start": start, "end": end, "text": text,
            "words": words if words is not None else
                     _w(text.split(), gap=0.05)}


def test_leading_orphan_because_merged_right():
    """jimmy 实测：'because' [116.33,117.33] 是 _split_by_gap 的碎片（1.35s
    真实停顿后才是 'you do take'），无句法完整性，应并回右段。"""
    segs = [
        _seg("why this moment is the most incredible moment", 113.07, 116.33),
        _seg("because", 116.33, 117.33),
        _seg("you do take", 118.68, 119.22),
    ]
    out = rejoin_leading_orphans(segs)
    assert len(out) == 2
    assert out[1]["text"] == "because you do take"
    assert out[1]["start"] == 116.33
    assert out[1]["end"] == 119.22
    # 时间窗取并集（词时间戳原样保留，不重算）
    assert [w["word"] for w in out[1]["words"]] == ["because", "you", "do", "take"]


def test_leading_orphan_kept_when_far():
    """jimmy 实测：'But I' [179.59,182.03] 距右段 1.89s >= 1.5s——
    停顿太长，孤儿是真正独立的（吞掉代价过大），保持独立。"""
    segs = [
        _seg("But I", 179.59, 182.03),
        _seg("gotta say this even after you see this special", 183.92, 188.90),
    ]
    out = rejoin_leading_orphans(segs)
    assert len(out) == 2
    assert out[0]["text"] == "But I"


def test_leading_orphan_kept_when_sentence():
    """以 [.!?] 结尾的真短句（'Yes.'）有句法完整性，不吞。"""
    segs = [
        _seg("Yes.", 10.0, 10.4),
        _seg("we did it", 10.6, 11.5),
    ]
    out = rejoin_leading_orphans(segs)
    assert len(out) == 2


def test_leading_orphan_comma_tail_still_merges():
    """逗号结尾（'So,'）仍是未完成句——并右自然（'So, what happened'）。"""
    segs = [
        _seg("So,", 5.0, 5.3),
        _seg("what happened out there", 5.5, 6.8),
    ]
    out = rejoin_leading_orphans(segs)
    assert len(out) == 1
    assert out[0]["text"] == "So, what happened out there"


def test_leading_orphan_respects_max_dur():
    """并右后 span 超过 max_dur(8s) → 保持独立（防长窗吞并）。"""
    segs = [
        _seg("but", 0.0, 0.5),
        _seg("that is a very long line that keeps going and going", 1.0, 9.0),
    ]
    out = rejoin_leading_orphans(segs)
    assert len(out) == 2


def test_leading_orphan_word_limit():
    """3 词以上不算句首孤儿（正常短句），不并。"""
    segs = [
        _seg("but I gotta", 0.0, 1.0),
        _seg("say this thing", 1.2, 2.5),
    ]
    out = rejoin_leading_orphans(segs)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# pipeline 集成：split_long_cues 产出直接验证（含 B 的接线）
# ---------------------------------------------------------------------------

def test_split_long_cues_smart_break_end_to_end():
    """端到端：jimmy #2 的整段 cue 走 split_long_cues，断句在逗号处。"""
    words = _w(["So", "we're", "at", "home", "for", "three", "months,",
                "we", "don't", "know", "if", "these", "jokes", "are",
                "going", "to", "work."])
    seg = {"start": words[0]["start"], "end": words[-1]["end"],
           "text": " ".join(w["word"] for w in words), "words": words}
    out = split_long_cues([seg], max_chars=42)
    assert len(out) == 2
    assert out[0]["text"].endswith("months,")
    assert out[0]["end"] == words[6]["end"]      # sub-cue 边界 = 词边界
    assert out[1]["start"] == words[7]["start"]
