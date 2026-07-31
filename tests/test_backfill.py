"""Tests for the backfill subcommand (Spec 10)."""
import json
import os

from video_translate.cli import EXIT_ARGS, EXIT_AWAITING_AGENT, EXIT_OK, main


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def test_backfill_empty_pending_returns_ok(tmp_path, capsys):
    pend = str(tmp_path / "pending.json")
    out = str(tmp_path / "zh.json")
    _write(pend, [])
    rc = main(["backfill", "--pending", pend, "--out", out])
    assert rc == EXIT_OK
    assert "nothing to do" in capsys.readouterr().out


def test_backfill_missing_pending_file_returns_args(tmp_path):
    rc = main(["backfill", "--pending", str(tmp_path / "nope.json"),
               "--out", str(tmp_path / "zh.json")])
    assert rc == EXIT_ARGS


def test_backfill_creates_task_and_returns_awaiting(tmp_path, capsys):
    pend = str(tmp_path / "pending.json")
    out = str(tmp_path / "selong.zh_segments.json")
    _write(pend, [
        {"index": 5, "start": 10.0, "end": 11.0, "text": "hello"},
        {"index": 12, "start": 24.0, "end": 25.0, "text": "world"},
    ])
    rc = main(["backfill", "--pending", pend, "--out", out])
    assert rc == EXIT_AWAITING_AGENT
    captured = capsys.readouterr().out
    assert "[AWAITING_AGENT]" in captured
    task = str(tmp_path / "selong.backfill_task.json")
    assert os.path.exists(task)
    data = json.load(open(task, encoding="utf-8"))
    # original indices preserved (NOT positional 0,1)
    idx = [x["index"] for x in data["batches"][0]["to_translate"]]
    assert idx == [5, 12]


def test_backfill_merge_then_generate(tmp_path, golden_segments_path):
    pend = str(tmp_path / "pending.json")
    out = str(tmp_path / "apollo_story.zh_segments.json")
    _write(pend, [{"index": 0, "start": 0.0, "end": 1.0, "text": "first"}])
    # existing zh missing index 0
    _write(out, {"1": "existing"})
    # agent fills index 0
    agent_zh = str(tmp_path / "agent.json")
    _write(agent_zh, {"0": "first-zh"})
    rc = main([
        "backfill", "--pending", pend, "--out", out,
        "--agent-zh", agent_zh,
        "--segments", golden_segments_path,
        "--outdir", str(tmp_path), "--base", "apollo_story",
    ])
    assert rc == EXIT_OK
    merged = json.load(open(out, encoding="utf-8"))
    assert merged["0"] == "first-zh"
    assert merged["1"] == "existing"  # preserved
    # generate ran -> bilingual.srt exists inside the per-video subfolder
    assert os.path.exists(os.path.join(str(tmp_path), "apollo_story",
                                       "apollo_story.bilingual.srt"))


def test_backfill_uses_same_prepare_as_translate_engine(tmp_path):
    """backfill and `translate --engine agent` both use prepare_translate_task."""
    from video_translate.translate import prepare_translate_task
    # (indirect) — backfill preserves indices via index_key; translate uses positional
    sp = str(tmp_path / "segs.json")
    tp1 = str(tmp_path / "t1.json")
    tp2 = str(tmp_path / "t2.json")
    _write(sp, [{"index": 9, "text": "x"}, {"index": 3, "text": "y"}])
    prepare_translate_task(sp, tp1, index_key="index", progress=lambda *_: None)
    prepare_translate_task(sp, tp2, progress=lambda *_: None)  # positional
    d1 = json.load(open(tp1, encoding="utf-8"))
    d2 = json.load(open(tp2, encoding="utf-8"))
    assert [x["index"] for x in d1["batches"][0]["to_translate"]] == [9, 3]
    assert [x["index"] for x in d2["batches"][0]["to_translate"]] == [0, 1]
