"""Control-plane Step 8 — run/transcribe state-chain integration.

Covers:
  - transcription completion persists the state chain (stage=translate +
    segments_sha anchor + n_segments);
  - run decisions are recorded with origin grading (explicit vs default);
  - every collaboration stop appends the NEXT block;
  - generate success records the generate/translate stages;
  - verify's state hook marks flagged / pending_agent outcomes.
"""
from __future__ import annotations

import argparse
import json

from video_translate import state as vt_state
from video_translate.cli import (
    EXIT_OK,
    _print_pipeline_next,
    _record_generate_stage,
    _record_run_decisions,
    _record_transcribe_stage,
    _verify_state_hook,
    main,
)

SEG = [{"start": 0.0, "end": 1.0, "text": "Hello there."}]


def _write_segments(tmp_path, base="demo"):
    d = tmp_path / base
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{base}.segments_en.json"
    p.write_text(json.dumps(SEG), encoding="utf-8")
    return str(p)


# --------------------------- transcribe chain ------------------------------

def test_record_transcribe_stage_writes_chain(tmp_path):
    seg = _write_segments(tmp_path)
    _record_transcribe_stage(seg, video="demo.mp4")
    st = vt_state.load(tmp_path, "demo")
    assert st, "state file must be written"
    assert vt_state.current_stage(st) == "translate"
    ts = vt_state.stage_status(st, "transcribe")
    assert ts["status"] == "ok"
    assert ts["segments_sha"] == vt_state.segment_sha(seg)
    assert ts["n_segments"] == len(SEG)
    assert st.get("video") == "demo.mp4"


def test_record_run_decisions_origin_grading(tmp_path):
    seg = _write_segments(tmp_path)
    args = argparse.Namespace(
        engine=None, style=None, align="whisperx",
        separate_vocals=False, vad=True, adaptive_vad=False)
    _record_run_decisions(args, seg)
    st = vt_state.load(tmp_path, "demo")
    dec = st["decisions"]
    assert dec["align"] == {"value": "whisperx", "origin": "explicit"}
    assert dec["vad"]["origin"] == "explicit" and dec["vad"]["value"] is True
    assert dec["engine"]["origin"] == "default"
    assert dec["style"]["origin"] == "default"
    assert dec["separate_vocals"]["origin"] == "default"


# --------------------------- NEXT blocks -----------------------------------

def test_print_pipeline_next_stop_point(tmp_path, capsys):
    _write_segments(tmp_path)
    _print_pipeline_next(str(tmp_path), "demo", video="demo.mp4")
    out = capsys.readouterr().out
    assert "[NEXT] stage=translate" in out
    assert "STOP POINT" in out


def test_generate_tail_prints_next(tmp_path, capsys, monkeypatch):
    seg = _write_segments(tmp_path)
    zh = tmp_path / "demo" / "demo.zh_segments.json"
    zh.parent.mkdir(parents=True, exist_ok=True)
    zh.write_text(json.dumps({"0": "你好呀。"}), encoding="utf-8")
    def _fake_generate(*a, **k):
        d = tmp_path / "demo"
        d.mkdir(parents=True, exist_ok=True)
        (d / "demo.bilingual.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
    monkeypatch.setattr(
        "video_translate.generate.generate_subtitles", _fake_generate)
    rc = main(["generate", "--segments", seg, "--zh", str(zh),
               "--outdir", str(tmp_path), "--base", "demo"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "[NEXT] stage=verify" in out
    st = vt_state.load(tmp_path, "demo")
    assert vt_state.stage_status(st, "generate")["status"] == "ok"
    assert vt_state.stage_status(st, "translate")["segments_sha"] \
        == vt_state.segment_sha(seg)


# --------------------------- verify state hook -----------------------------

def test_verify_hook_marks_flagged_and_pending(tmp_path):
    seg = _write_segments(tmp_path)
    _verify_state_hook(seg, "flagged")
    st = vt_state.load(tmp_path, "demo")
    assert vt_state.stage_status(st, "verify")["status"] == "flagged"
    assert vt_state.current_stage(st) == "verify"


def test_verify_hook_pending_agent(tmp_path):
    seg = _write_segments(tmp_path)
    _verify_state_hook(seg, "pending_agent")
    st = vt_state.load(tmp_path, "demo")
    assert vt_state.stage_status(st, "verify")["status"] == "pending_agent"
    assert vt_state.current_stage(st) == "verify"


def test_verify_hook_tolerates_missing_state_dir(tmp_path):
    seg = _write_segments(tmp_path)
    (tmp_path / "demo.vt_state.json").unlink(missing_ok=True)
    # ensure_state rebuilds; the hook itself must never raise
    _verify_state_hook(seg, "ok")
    st = vt_state.load(tmp_path, "demo")
    assert vt_state.stage_status(st, "verify")["status"] == "ok"


# --------------------------- _record_generate_stage ------------------------

def test_record_generate_stage_direct(tmp_path):
    seg = _write_segments(tmp_path)
    _record_generate_stage(seg, str(tmp_path), "demo", style="film")
    st = vt_state.load(tmp_path, "demo")
    assert vt_state.stage_status(st, "generate")["style"] == "film"
    assert vt_state.current_stage(st) == "verify"


# --------------------------- resegment refreshes the anchor ----------------

def test_resegment_refreshes_segments_sha_anchor(tmp_path, monkeypatch):
    """resegment 是转写层的合法修正：splice 后必须刷新 segments_sha 基线，
    否则 generate 的陈旧闸会把「已重译」误判为「忘重译」（cp 缺口修复）。"""
    seg = _write_segments(tmp_path)  # 1 段基线
    _record_transcribe_stage(seg, video="demo.mp4")
    old_sha = vt_state.segment_sha(seg)

    # 模拟 resegment：窗口重解码产出新段（函数内 import，patch 源模块即可）
    new_window_seg = [{"start": 2.0, "end": 3.0, "text": "Inserted line."}]
    monkeypatch.setattr(
        "video_translate.transcribe.transcribe_window",
        lambda *a, **k: [dict(s) for s in new_window_seg])
    args = argparse.Namespace(
        segments=seg, video="demo.mp4", windows=["2.0-3.0"], lang="en",
        vad=False, model=None, threads=4, device=None, compute_type=None,
        separate_vocals=None, demucs_model=None)
    from video_translate.cli import cmd_resegment
    assert cmd_resegment(args) == EXIT_OK

    # 段已 splice（1 + 1 = 2 段）
    merged = json.loads(open(seg, encoding="utf-8").read())
    assert len(merged) == 2
    # 指纹基线已刷新为新文件，且链位停在 translate 等待重译
    st = vt_state.load(tmp_path, "demo")
    assert vt_state.stage_status(st, "transcribe")["segments_sha"] \
        == vt_state.segment_sha(seg) != old_sha
    assert vt_state.current_stage(st) == "translate"
    # 由此 generate 的 sha 闸放行（zh 重译后）
    zh = tmp_path / "demo" / "demo.zh_segments.json"
    zh.parent.mkdir(parents=True, exist_ok=True)
    zh.write_text(
        json.dumps({0: "原句。", 1: "插句。"}), encoding="utf-8")
    from video_translate import pipeline
    pipeline.enforce("generate", pipeline.build_ctx(tmp_path, "demo"),
                     caps_probe=lambda n: True)  # 不 raise 即放行


# ----------------------- ADR-031 D6: decisions survive rebuild --------------

def test_ensure_state_preserves_decisions_on_stale_sha(tmp_path):
    """segments 文件变化（resegment 合法修订）触发 rebuild 时，decisions 必须保留。

    kathy_meta_vlog 事故：run 记录的 decisions 在首轮 resegment 后被
    ensure_state 的 rebuild 启发式整档抹掉（实测 vt_state.json decisions == {}）。
    """
    seg = _write_segments(tmp_path)
    args = argparse.Namespace(
        engine=None, style=None, align="whisperx",
        separate_vocals=False, vad=True, adaptive_vad=False)
    _record_run_decisions(args, seg)

    # 模拟 resegment：segments 文件被合法修订 -> sha 陈旧 -> rebuild 触发
    segs = json.loads(open(seg, encoding="utf-8").read())
    segs.append({"start": 2.0, "end": 3.0, "text": "Amended in."})
    with open(seg, "w", encoding="utf-8") as f:
        f.write(json.dumps(segs))

    st = vt_state.ensure_state(tmp_path, "demo")
    assert st["decisions"]["align"]["value"] == "whisperx"       # 保留
    assert vt_state.stage_status(st, "transcribe")["segments_sha"] \
        == vt_state.segment_sha(seg)                             # 且锚点已刷新


def test_ensure_state_missing_state_rebuilds_without_decisions(tmp_path):
    """回归守卫：state 缺失时照旧从产物重建（decisions 为空、锚点刷新）。"""
    seg = _write_segments(tmp_path)
    st = vt_state.ensure_state(tmp_path, "demo")
    assert st["decisions"] == {}
    assert vt_state.stage_status(st, "transcribe")["segments_sha"] \
        == vt_state.segment_sha(seg)


def test_ensure_state_preserves_video_path_on_stale_sha(tmp_path):
    """video 路径同理保留（run 之后的 resegment 不该把 video 字段抹掉）。"""
    seg = _write_segments(tmp_path)
    _record_transcribe_stage(seg, video="demo.mp4")
    segs = json.loads(open(seg, encoding="utf-8").read())
    segs.append({"start": 2.0, "end": 3.0, "text": "Amended in."})
    with open(seg, "w", encoding="utf-8") as f:
        f.write(json.dumps(segs))
    st = vt_state.ensure_state(tmp_path, "demo")
    assert st.get("video") == "demo.mp4"