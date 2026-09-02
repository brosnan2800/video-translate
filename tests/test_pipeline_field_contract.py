"""ADR-035 M3 — 跨阶段字段契约测试（验收闸）。

事故回归：merge 白名单重建曾把主通路段的 no_speech_prob / avg_logprob /
compression_ratio 剥掉 → review 的 G1 与 verify 低置信道在合并后时间轴"失明"。
Z2 之后本测试断言全链字段存活且可回查：

  transcribe(raw, 全字段) → apply_merge(视图 + _raw_indices)
    → raw 置信度永不丢 → review/verify 按索引回查 → 信号 A 复明。
"""
from __future__ import annotations

import json

import pytest

from video_translate.artifacts import raw_sources, validate_artifact
from video_translate.merge import apply_merge, attach_raw_indices
from video_translate.review import MISSING, review_segments
from video_translate.verify import LOW_CONFIDENCE, find_low_confidence_segments


def _seg(start: float, dur: float, text: str, *,
         nsp: float = 0.05, alp: float = -0.5) -> dict:
    """合成 whisper 段：全字段（结构 + 三个置信度字段），词时间戳均匀切分。"""
    tokens = text.split()
    step = dur / max(len(tokens), 1)
    words = [{"word": f" {t}", "start": round(start + k * step, 3),
              "end": round(start + (k + 1) * step, 3)}
             for k, t in enumerate(tokens)]
    return {"start": start, "end": round(start + dur, 3), "text": text,
            "words": words, "no_speech_prob": nsp, "avg_logprob": alp,
            "compression_ratio": 1.4}


# --------------------------------------------------------------------------- #
# attach_raw_indices 纯函数：合并 / 切分 两种映射
# --------------------------------------------------------------------------- #

def test_attach_raw_indices_grouping():
    raw = [_seg(0.0, 1.0, "one two"),
           _seg(1.2, 1.0, "three four"),
           _seg(2.4, 1.0, "five six")]
    merged = [{"start": 0.0, "end": 2.2, "text": "one two three four",
               "words": raw[0]["words"] + raw[1]["words"]}]
    out = attach_raw_indices(merged, raw)
    assert out[0]["_raw_indices"] == [0, 1]


def test_attach_raw_indices_split_maps_to_same_parent():
    raw = [_seg(0.0, 4.0, "a b c d")]
    # split: 一条 raw 段切成两条子 cue，均映射到同一父段
    cues = [{"start": 0.0, "end": 1.8, "text": "a b",
             "words": raw[0]["words"][:2]},
            {"start": 2.2, "end": 4.0, "text": "c d",
             "words": raw[0]["words"][2:]}]
    out = attach_raw_indices(cues, raw)
    assert out[0]["_raw_indices"] == [0]
    assert out[1]["_raw_indices"] == [0]


def test_attach_raw_indices_preserves_unknown_fields():
    raw = [_seg(0.0, 1.0, "x y")]
    out = attach_raw_indices([{"start": 0.0, "end": 1.0, "text": "x y",
                               "_custom": 7}], raw)
    assert out[0]["_custom"] == 7  # copy-then-override：未知字段保留


# --------------------------------------------------------------------------- #
# 端到端链：raw → apply_merge → 视图 + 回查
# --------------------------------------------------------------------------- #

@pytest.fixture()
def merged_chain(tmp_path):
    """4 段 raw（1 段高 no_speech 可疑：笑声下真音的典型指纹）→ apply_merge。"""
    raw = [
        _seg(0.0, 1.0, "keep your head"),
        _seg(1.2, 1.0, "when all about"),
        # nsp=0.72 且 alp=-0.65：merge 幻觉过滤器不丢（alp 未低于 -1.0），
        # 但 review 信号 A 应判可疑 —— 正是笑声下真音被合并吞掉的场景。
        _seg(2.4, 1.2, "muffled words under laughter", nsp=0.72, alp=-0.65),
        _seg(3.8, 1.0, "you will be a man"),
    ]
    segs_path = tmp_path / "clip.segments_en.json"
    raw_path = tmp_path / "clip.segments_raw.json"
    segs_path.write_text(json.dumps(raw), encoding="utf-8")
    apply_merge(str(segs_path), raw_path=str(raw_path))
    merged = json.loads(segs_path.read_text(encoding="utf-8"))
    raw_saved = json.loads(raw_path.read_text(encoding="utf-8"))
    return merged, raw_saved


def test_raw_keeps_confidence_fields(merged_chain):
    _, raw_saved = merged_chain
    # 置信度跟段绑死在 raw，永不丢（契约 require_carry 全绿）
    assert validate_artifact("segments_raw", raw_saved, require_carry=True) == []


def test_view_carries_raw_indices_and_structure(merged_chain):
    merged, _ = merged_chain
    assert merged, "apply_merge 必须产出合并视图"
    assert all("_raw_indices" in m for m in merged)
    assert validate_artifact("segments", merged) == []
    # 可疑源段的下标出现在某个视图段的回查指针里
    assert any(2 in m["_raw_indices"] for m in merged)


def test_raw_lookup_returns_suspect_source(merged_chain):
    merged, raw_saved = merged_chain
    for m in merged:
        if 2 in m["_raw_indices"]:
            srcs = raw_sources(m, raw_saved)
            assert any(abs(float(s["no_speech_prob"]) - 0.72) < 1e-9
                       for s in srcs)


def test_review_signal_a_survives_merge_via_raw_lookup(merged_chain):
    merged, raw_saved = merged_chain
    recs = review_segments(merged, [], raw_segments=raw_saved)
    missing = [r for r in recs if r["verdict"] == MISSING]
    assert missing, "合并后时间轴上的可疑段必须被判 MISSING（G1 信号 A 复明）"
    assert any(r.startswith("raw#") for r in missing[0]["reasons"])
    # 事故回归对照：无 raw 回查 = 信号 A 失明，判不出 MISSING（二期休眠根因）
    recs_legacy = review_segments(merged, [])
    assert all(r["verdict"] != MISSING for r in recs_legacy)


def test_verify_low_confidence_lane_sees_through_merge(merged_chain):
    merged, raw_saved = merged_chain
    issues = find_low_confidence_segments(merged, raw_segments=raw_saved)
    assert issues, "低置信道必须能穿透合并视图看到 raw 源段的可疑值"
    assert issues[0]["type"] == LOW_CONFIDENCE
    assert issues[0]["no_speech_prob"] == pytest.approx(0.72)
    # 事故回归对照：无 raw = 该道全盲（merge 丢字段的历史行为）
    assert find_low_confidence_segments(merged) == []


def test_verify_low_confidence_own_fields_still_work():
    """恢复段等自带置信度的段：无指针时按自身字段判定（向后兼容）。"""
    segs = [_seg(0.0, 1.0, "hallucinated words", nsp=0.9, alp=-0.4)]
    issues = find_low_confidence_segments(segs)
    assert len(issues) == 1
    assert issues[0]["no_speech_prob"] == pytest.approx(0.9)


def test_raw_lookup_out_of_range_is_tolerated():
    view = {"start": 0.0, "end": 1.0, "text": "x", "_raw_indices": [5, 9]}
    assert raw_sources(view, [_seg(0.0, 1.0, "x")]) == []
    assert raw_sources(view, None) == []
    no_ptr = {"start": 0.0, "end": 1.0, "text": "x"}
    assert raw_sources(no_ptr, [_seg(0.0, 1.0, "x")]) == []
