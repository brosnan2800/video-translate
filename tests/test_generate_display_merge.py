"""Spec 26 — display-layer short-cue merge (pure-function + st-tail regression).

Tests the merge WITHOUT touching segments/words/start/end (presentation layer only).
The st tail (cues 246-262, real geometry from st.mp4) is replayed as 17 synthetic
segments so the regression is self-contained and deterministic.
"""
import json

from video_translate.generate import (
    build_outputs, merge_display_cues, DEFAULT_DM_GAP, DEFAULT_DM_MAX_DUR,
    DEFAULT_DM_MAX_CHARS, DEFAULT_DM_MAX_ZH,
)
from video_translate.artifacts import validate_artifact


def _seg(start, end, en, zh):
    return {"start": start, "end": end, "text": en, "zh": zh}


def _bounds(segs):
    return [[s["start"], s["end"]] for s in segs]


# --- st tail geometry (cue 246..262): gaps + short/long flags ------------------
# gaps: 0.20 1.21 0.20 0.20 0.20 0.20 0.20 0.77 0.42 1.79 1.52 0.60 0.55 2.14 0.59 1.24
# durs:  246=0.46 247=1.18 248=0.77 249=0.82 250=1.26 251=1.62 252=0.44 253=1.50
#        254=1.03 255=1.00 256=1.05 257=1.76 258=1.32 259=1.00 260=2.17(NOT short)
#        261=1.00 262=2.61(NOT short)
_TAIL_EN = [
    "BECAUSE I DON'T KNOW WHO YOU", "who you think you are.",
    "YOU'RE NOT IT, BROTHER.", "YOU'RE NOT THE REASON.",
    "HE WOULDN'T HAVE TOLD EVERYBODY HE WAS THE REASON",
    "FOR MY SUCCESS, BUT YOU'RE NOT.",
    "I MADE HIM.", "WE'LL MAKE ANOTHER ONE.", "I GOT BETTER IDEA.",
    "MAKE YOURSELF.", "JUST GO MAKE YOU.", "IF YOU KNOW HOW TO MAKE A STAR,",
    "MAKE YOURSELF ONE.", "IT PAY GOOD.",
    "YOU AIN'T EVEN GOT TO GO FIND A CLIENT.", "JUST DO YOU.",
    "SO, YOU KNOW, MAN, I just that's my that's my thing",
]
_TAIL_ZH = [
    "因为我真不知道你", "你以为你是谁。", "你不是那个人，兄弟。", "你不是原因。",
    "他要是真行，早该到处说自己是我成功的", "原因了。但你，不是。",
    "他是我造出来的。", "我们还能再造一个。", "我有个更好的主意。",
    "你自己造一个吧。", "去把你自己造出来。", "你要是会造明星，",
    "就造你自己。", "这活儿很赚钱。",
    "你连客户都不用去找。", "做好你自己就行。", "所以，你知道，兄弟，我就是——这就是我的那套，我的事儿。",
]


def _tail_segments():
    starts = [620.00, 620.66, 623.05, 624.02, 625.04, 626.50, 628.32, 628.96,
              631.23, 632.68, 635.47, 638.04, 640.40, 642.27, 645.41, 648.17,
              650.41]
    durs = [0.46, 1.18, 0.77, 0.82, 1.26, 1.62, 0.44, 1.50, 1.03, 1.00, 1.05,
            1.76, 1.32, 1.00, 2.17, 1.00, 2.61]
    segs = []
    for i, (s, d) in enumerate(zip(starts, durs)):
        segs.append(_seg(round(s, 2), round(s + d, 2), _TAIL_EN[i], _TAIL_ZH[i]))
    return segs


def _tail_zh():
    return {i: _TAIL_ZH[i] for i in range(len(_TAIL_ZH))}


# --- pure-function unit tests -------------------------------------------------

def test_merge_gap_boundary():
    segs = [_seg(0.0, 1.0, "a", "甲"), _seg(1.2, 2.2, "b", "乙"),
            _seg(3.1, 4.1, "c", "丙")]
    bounds = _bounds(segs)
    groups, _ = merge_display_cues(bounds, segs, {0: "甲", 1: "乙", 2: "丙"},
                                   gap=0.8)
    # gap 1.2.0->1.2 = 0.2 merges; 2.2->3.1 = 0.9 doesn't
    assert groups == [[0, 1], [2]]


def test_long_cue_does_not_absorb_short_neighbor():
    # group_short gating: a long (non-short) cue starts a non-mergeable group,
    # so the following short cue stays separate (no swallowing).
    segs = [_seg(0.0, 3.0, "long line here", "长句"),
            _seg(3.2, 3.8, "short", "短")]
    bounds = _bounds(segs)
    groups, _ = merge_display_cues(bounds, segs, {0: "长句", 1: "短"}, gap=0.8)
    assert groups == [[0], [1]]


def test_en_overflow_refuses_third_cue():
    a = "x" * 40
    b = "y" * 40
    c = "z" * 10
    segs = [_seg(0.0, 1.0, a, ""), _seg(1.2, 2.2, b, ""),
            _seg(2.4, 3.4, c, "")]
    bounds = _bounds(segs)
    groups, rej = merge_display_cues(bounds, segs, {0: "", 1: "", 2: ""}, gap=0.8)
    # a+b fits (<=84), c pushes over 84 -> rejected
    assert groups == [[0, 1], [2]]
    assert any(r["reason"] == "en-overflow" for r in rej)


def test_dur_overflow_refuses_third_cue():
    # all three are short; the third's span from group start exceeds max_dur.
    segs = [_seg(0.0, 1.0, "a", ""), _seg(1.2, 2.2, "b", ""),
            _seg(2.4, 3.2, "c", "")]  # span 0.0->3.2 > max_dur 3.0
    bounds = _bounds(segs)
    groups, rej = merge_display_cues(bounds, segs, {0: "", 1: "", 2: ""},
                                     gap=0.8, max_dur=3.0)
    assert groups == [[0, 1], [2]]
    assert any(r["reason"] == "dur-overflow" for r in rej)


def test_display_merge_default_off_is_noop():
    # gap 1.0s > dm_gap 0.8 -> neither path merges -> identical output
    segs = [_seg(0.0, 1.0, "a", "甲"), _seg(2.0, 3.0, "b", "乙")]
    bounds = _bounds(segs)
    zh = {0: "甲", 1: "乙"}
    off = build_outputs(segs, zh, gap=0.2, display_merge=False)
    on = build_outputs(segs, zh, gap=0.2, display_merge=True,
                       dm_gap=0.8)
    assert off[".bilingual.srt"] == on[".bilingual.srt"]


# --- st-tail regression -------------------------------------------------------

def test_st_tail_merges_to_nine_groups():
    segs = _tail_segments()
    bounds = _bounds(segs)
    zh = _tail_zh()
    groups, rejected = merge_display_cues(bounds, segs, zh, gap=0.8)
    # 17 cues -> 9 display groups
    assert len(groups) == 9
    # first two cues (246/247) merge
    assert groups[0] == [0, 1]
    # a long chain (>=3) formed somewhere (overflow-free short run)
    assert any(len(g) >= 3 for g in groups)
    # 260 (idx14, non-short) and 261 (idx15) never share a group
    grp14 = next(g for g in groups if 14 in g)
    grp15 = next(g for g in groups if 15 in g)
    assert grp14 != grp15
    assert len(grp14) == 1 and len(grp15) == 1
    # 256 (idx10) and 262 (idx16) are standalone (both-side gap > 0.8)
    assert len(next(g for g in groups if 10 in g)) == 1
    assert len(next(g for g in groups if 16 in g)) == 1
    # audit trail recorded at least one gap rejection
    assert any(r["reason"].startswith("gap=") for r in rejected)


def test_st_tail_integration_cue_count_and_text():
    segs = _tail_segments()
    zh = _tail_zh()
    out = build_outputs(segs, zh, gap=0.2, display_merge=True, dm_gap=0.8)
    # 9 cues in the bilingual track
    assert out[".bilingual.srt"].count("-->") == 9
    # merged cue carries concatenated zh of 246+247
    assert "因为我真不知道你" in out[".bilingual.srt"]
    assert "你以为你是谁。" in out[".bilingual.srt"]


def test_display_merge_sidecar_contract():
    segs = _tail_segments()
    bounds = _bounds(segs)
    zh = _tail_zh()
    groups, rejected = merge_display_cues(bounds, segs, zh, gap=0.8)
    merged_bounds = [[bounds[g[0]][0], bounds[g[-1]][1]] for g in groups]
    audit = {
        "params": {"display_merge": True, "gap": 0.8, "max_dur": DEFAULT_DM_MAX_DUR,
                   "max_chars": DEFAULT_DM_MAX_CHARS, "max_zh": DEFAULT_DM_MAX_ZH,
                   "short_dur": 2.0, "short_words": 8, "min_dur": 0.0,
                   "tail": 0.0, "offset": 0.0},
        "total_segments": len(segs),
        "display_cues": len(groups),
        "merged_groups": sum(1 for g in groups if len(g) > 1),
        "groups": [
            {"cue": k + 1, "source_indices": g,
             "window": [round(merged_bounds[k][0], 3),
                        round(merged_bounds[k][1], 3)],
             "zh": "".join(zh[j] for j in g),
             "en": " ".join(segs[j]["text"] for j in g)}
            for k, g in enumerate(groups) if len(g) > 1
        ],
        "rejected": rejected,
    }
    # schema validates (no required fields missing)
    assert validate_artifact("display_merge", audit) == []
    # every source index covered exactly once
    flat = [i for g in groups for i in g]
    assert sorted(flat) == list(range(len(segs)))


def test_display_merge_artifact_registered():
    from video_translate.artifacts import get_artifact
    spec = get_artifact("display_merge")
    assert spec["produced_by"] == "generate"
    assert "verify" in spec["consumed_by"]
