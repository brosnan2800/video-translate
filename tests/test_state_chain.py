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
    p = tmp_path / f"{base}.segments_en.json"
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
    zh = tmp_path / "demo.zh_segments.json"
    zh.write_text(json.dumps({"0": "你好呀。"}), encoding="utf-8")
    monkeypatch.setattr(
        "video_translate.generate.generate_subtitles",
        lambda *a, **k: (tmp_path / "demo.bilingual.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8"))
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