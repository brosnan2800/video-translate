"""ADR-031 D2-D7 — verify 声学 lane 硬化 + 恢复段可见化 + BGM 能量分级。

kathy_meta_vlog 事故固化（全部为真实实测几何）：
  - "We'll be right back."(nsp=0.906) / "Wait."(nsp=0.851) 逃过全部防线 ->
    ~~verify 增加段级置信度巡检（D3）~~ **D3 已由 ADR-041 移除**（自证，非独立验证）；
    verify 声学 lane 自此只依赖 FFmpeg 独立参照与 cue 的客观几何；
  - fill_gaps 恢复段 "Is he not going to make it?" 的 "Is"(39.96-40.34) 骑在
    前段 "busy."(39.93-40.16) 上、段重叠 0.2s -> verify 增加相邻段重叠/词碰撞
    巡检（D4/D5，只报告不自动修剪——ADR-012 声学红线）；
  - 恢复段在语义回读 task 中无标记，agent 只能靠语义合理性放行 -> task 对恢复段
    附 suspect+hint（D2）；
  - uncovered 窗的 BGM/语音判定原先靠 agent 手工 volumedetect -> verify 自动按
    人声轨能量分级（D7，阈值与轮 2 人工仲裁同标准）。

全部纯函数（no subprocess）——subprocess 层归 audio_profile.probe_volume_window。
"""
from video_translate.verify import (
    ADJACENT_OVERLAP, build_semantic_reread_task,
    classify_uncovered_windows, classify_vocals_energy,
    find_adjacent_overlaps, is_recovered_segment,
)


# --------------------------- D2: recovered visibility -----------------------

def test_is_recovered_segment():
    assert is_recovered_segment({"_recovered": True}) is True
    assert is_recovered_segment({"origin": "resegment"}) is True
    assert is_recovered_segment({"origin": "whisper"}) is False
    assert is_recovered_segment({"text": "plain whisper seg"}) is False


def test_reread_task_marks_recovered_pairs_suspect():
    segs = [
        {"start": 0.0, "end": 1.0, "text": "Hello there."},
        {"start": 1.4, "end": 2.4, "text": "We'll be right back.",
         "_recovered": True},
        {"start": 2.8, "end": 4.0, "text": "Spliced line.",
         "origin": "resegment"},
    ]
    task = build_semantic_reread_task(
        segs, {0: "你好呀。", 1: "我们马上回来哈。", 2: "拼进来的句子。"})
    pairs = {p["index"]: p for p in task["pairs"]}
    assert "suspect" not in pairs[0]            # 主转写段不带嫌疑标记
    assert pairs[1]["suspect"] is True
    assert "hallucination" in pairs[1]["hint"]
    assert pairs[2]["suspect"] is True          # origin=resegment 同样算恢复段


# --------------------------- D4/D5: adjacent overlap + prefix ---------------

# 注：D3「低置信道」（`find_low_confidence_segments`）已由 ADR-041 整体移除 ——
# 它用 ASR 模型自身的评分字段（no_speech_prob / avg_logprob）巡检 ASR 自己的产物，
# 属「自证」而非独立验证。该能力归位到① ASR 层内部（幻觉过滤 + review 的 A∩B
# 重处理判定），相关回归见 tests/test_merge.py 与 tests/test_pipeline_field_contract.py。

def test_find_adjacent_overlaps_flags_real_accident_geometry():
    """真实事故几何：Super busy.[39.26-40.16] vs Is he...[39.96-43.94] 重叠 0.2s。"""
    segs = [
        {"start": 39.26, "end": 40.16, "text": "Super busy.",
         "words": [{"start": 39.26, "end": 39.89, "word": "Super"},
                   {"start": 39.93, "end": 40.16, "word": "busy."}]},
        {"start": 39.96, "end": 43.94, "text": "Is he not going to make it?",
         "_recovered": True,
         "words": [{"start": 39.96, "end": 40.34, "word": "Is"},
                   {"start": 40.60, "end": 43.94, "word": "it?"}]},
    ]
    issues = find_adjacent_overlaps(segs)
    assert len(issues) == 1
    it = issues[0]
    assert it["type"] == ADJACENT_OVERLAP
    assert it["index_a"] == 0 and it["index_b"] == 1
    assert abs(it["overlap"] - 0.2) < 1e-9
    assert it["word_collision"] is True          # "Is" 骑在 "busy." 上
    assert "hallucinated prefix" in it["hint"]   # D5: 恢复段前缀幻觉指纹


def test_find_adjacent_overlaps_tolerates_fuzzy_boundaries():
    """0.03s 的邻接模糊边界（真实语音常见）不 flag。"""
    segs = [
        {"start": 10.0, "end": 11.0, "text": "a.",
         "words": [{"start": 10.0, "end": 11.0, "word": "a."}]},
        {"start": 10.97, "end": 12.0, "text": "b.",
         "words": [{"start": 10.97, "end": 12.0, "word": "b."}]},
    ]
    assert find_adjacent_overlaps(segs) == []


def test_find_adjacent_overlaps_word_collision_without_recovered():
    """非恢复段的词碰撞：报 word_collision，但不给幻觉前缀 hint。"""
    segs = [
        {"start": 0.0, "end": 1.0, "text": "a",
         "words": [{"start": 0.0, "end": 1.0, "word": "a"}]},
        {"start": 0.9, "end": 2.0, "text": "b",
         "words": [{"start": 0.9, "end": 2.0, "word": "b"}]},
    ]
    issues = find_adjacent_overlaps(segs)
    assert len(issues) == 1
    assert issues[0]["word_collision"] is True
    assert "hint" not in issues[0]


def test_find_adjacent_overlaps_needs_word_timestamps_for_collision():
    """无词级数据时不做词碰撞判定，但段级重叠仍然 flag。"""
    segs = [
        {"start": 0.0, "end": 1.0, "text": "a"},
        {"start": 0.9, "end": 2.0, "text": "b"},
    ]
    issues = find_adjacent_overlaps(segs)
    assert len(issues) == 1
    assert "word_collision" not in issues[0]


# --------------------------- D7: BGM energy classification ------------------

def test_classify_vocals_energy_thresholds():
    """阈值用 kathy_meta_vlog 实测标定值校准。"""
    assert classify_vocals_energy(-40.0, -22.0) == "bgm"        # BGM 残响带
    assert classify_vocals_energy(-36.6, -22.6) == "bgm"        # 边界内（轮 2 仲裁同标准）
    assert classify_vocals_energy(-19.8, -6.1) == "speech"      # 确认语音基线
    assert classify_vocals_energy(-30.0, -11.0) == "speech"     # max 峰兜底
    assert classify_vocals_energy(-30.0, -20.0) == "ambiguous"  # 中间带 -> 人工
    assert classify_vocals_energy(None, None) == "unknown"      # 探测失败


def test_classify_uncovered_windows_attaches_suggestions():
    uncovered = [(17.47, 18.87), (45.0, 52.5)]
    volumes = {(17.47, 18.87): (-40.0, -22.0), (45.0, 52.5): (-18.0, -5.0)}
    out = classify_uncovered_windows(uncovered, volumes)
    assert out[(17.47, 18.87)]["verdict"] == "bgm"
    assert "resegment this window" not in out[(17.47, 18.87)]["suggestion"]
    assert out[(45.0, 52.5)]["verdict"] == "speech"
    assert "resegment this window" in out[(45.0, 52.5)]["suggestion"]


def test_classify_uncovered_windows_unknown_on_probe_failure():
    out = classify_uncovered_windows([(1.0, 3.0)], {(1.0, 3.0): (None, None)})
    assert out[(1.0, 3.0)]["verdict"] == "unknown"
    assert "manually" in out[(1.0, 3.0)]["suggestion"]


def test_classify_uncovered_windows_missing_entry_is_unknown():
    out = classify_uncovered_windows([(1.0, 3.0)], {})
    assert out[(1.0, 3.0)]["verdict"] == "unknown"