"""Tests for the agent translation engine (Spec 09).

No network, no LLM client: prepare_translate_task / validate_zh / merge_agent_zh
are pure file operations — the calling agent does the actual translation.
"""
import json
import os

from video_translate.translate import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONTEXT_WINDOW,
    merge_agent_zh,
    prepare_translate_task,
    validate_zh,
)


def _write_segs(path, n=10):
    segs = [{"start": i, "end": i + 1, "text": f"seg {i}"} for i in range(n)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(segs, f)


def test_prepare_task_creates_file(tmp_path):
    sp = str(tmp_path / "segs.json")
    tp = str(tmp_path / "task.json")
    _write_segs(sp)
    prepare_translate_task(sp, tp, progress=lambda *_: None)
    assert os.path.exists(tp)
    task = json.load(open(tp, encoding="utf-8"))
    assert task["version"] == 1
    assert "persona" in task
    assert "output_schema" in task
    assert "batches" in task


def test_prepare_task_batch_size_default(tmp_path):
    sp = str(tmp_path / "segs.json")
    tp = str(tmp_path / "task.json")
    _write_segs(sp, n=10)  # 10 segs / batch 8 -> 2 batches
    prepare_translate_task(sp, tp, progress=lambda *_: None)
    task = json.load(open(tp, encoding="utf-8"))
    assert len(task["batches"]) == 2
    assert DEFAULT_BATCH_SIZE == 8


def test_prepare_task_context_window(tmp_path):
    sp = str(tmp_path / "segs.json")
    tp = str(tmp_path / "task.json")
    _write_segs(sp, n=10)
    prepare_translate_task(sp, tp, progress=lambda *_: None)
    task = json.load(open(tp, encoding="utf-8"))
    b0 = task["batches"][0]
    # first batch: to_translate [0..7], context_before=[], context_after=[8,9]
    assert [x["index"] for x in b0["to_translate"]] == [0, 1, 2, 3, 4, 5, 6, 7]
    assert b0["context_before"] == []
    assert [x["index"] for x in b0["context_after"]] == [8, 9]
    b1 = task["batches"][1]
    # second batch: to_translate [8,9], context_before=[6,7], context_after=[]
    assert [x["index"] for x in b1["to_translate"]] == [8, 9]
    assert [x["index"] for x in b1["context_before"]] == [6, 7]
    assert b1["context_after"] == []
    assert DEFAULT_CONTEXT_WINDOW == 2


def test_prepare_task_persona_embedded(tmp_path):
    sp = str(tmp_path / "segs.json")
    tp = str(tmp_path / "task.json")
    _write_segs(sp, n=3)
    prepare_translate_task(sp, tp, persona="custom persona xyz", progress=lambda *_: None)
    task = json.load(open(tp, encoding="utf-8"))
    assert task["persona"] == "custom persona xyz"


def test_prepare_task_to_translate_indices(tmp_path):
    sp = str(tmp_path / "segs.json")
    tp = str(tmp_path / "task.json")
    _write_segs(sp, n=20)
    prepare_translate_task(sp, tp, batch_size=5, progress=lambda *_: None)
    task = json.load(open(tp, encoding="utf-8"))
    # 4 batches of 5; indices contiguous and partition [0,20)
    all_idx = []
    for b in task["batches"]:
        all_idx.extend(x["index"] for x in b["to_translate"])
    assert all_idx == list(range(20))


def test_validate_zh_complete(tmp_path):
    sp = str(tmp_path / "segs.json")
    zp = str(tmp_path / "zh.json")
    _write_segs(sp, n=3)
    with open(zp, "w", encoding="utf-8") as f:
        json.dump({"0": "甲", "1": "乙", "2": "丙"}, f)
    ok, missing = validate_zh(sp, zp, progress=lambda *_: None)
    assert ok is True
    assert missing == []


def test_validate_zh_missing(tmp_path):
    sp = str(tmp_path / "segs.json")
    zp = str(tmp_path / "zh.json")
    _write_segs(sp, n=3)
    with open(zp, "w", encoding="utf-8") as f:
        json.dump({"0": "甲"}, f)
    ok, missing = validate_zh(sp, zp, progress=lambda *_: None)
    assert ok is False
    assert missing == [1, 2]


def test_merge_agent_zh_preserves_existing(tmp_path):
    zp = str(tmp_path / "zh.json")
    ap = str(tmp_path / "agent.json")
    with open(zp, "w", encoding="utf-8") as f:
        json.dump({"0": "a", "1": "b"}, f)
    with open(ap, "w", encoding="utf-8") as f:
        json.dump({"1": "B", "2": "c"}, f)
    merged = merge_agent_zh(zp, ap, progress=lambda *_: None)
    assert merged == {"0": "a", "1": "B", "2": "c"}
    # persisted
    assert json.load(open(zp, encoding="utf-8")) == {"0": "a", "1": "B", "2": "c"}


def test_merge_agent_zh_str_keys_normalized(tmp_path):
    zp = str(tmp_path / "zh.json")
    ap = str(tmp_path / "agent.json")
    with open(zp, "w", encoding="utf-8") as f:
        json.dump({}, f)
    # agent may write int keys
    with open(ap, "w", encoding="utf-8") as f:
        json.dump({0: "x", 1: "y"}, f)
    merged = merge_agent_zh(zp, ap, progress=lambda *_: None)
    assert set(merged.keys()) == {"0", "1"}
