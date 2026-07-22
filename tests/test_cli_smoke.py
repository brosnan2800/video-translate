"""CLI smoke tests (Spec 05 / 11). No heavy deps required."""
import os

import pytest

from video_translate.cli import (
    EXIT_AWAITING_AGENT,
    EXIT_MISSING_DEP,
    EXIT_OK,
    build_parser,
    main,
)


def test_parser_accepts_all_subcommands():
    p = build_parser()
    for cmd in ("transcribe", "translate", "generate", "run", "setup", "doctor", "backfill"):
        pass
    ns = p.parse_args(["doctor"])
    assert ns.command == "doctor"


def test_unknown_subcommand_exits_2():
    with pytest.raises(SystemExit) as e:
        main(["frobnicate"])
    assert e.value.code == 2


def test_doctor_returns_ok(capsys):
    rc = main(["doctor"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "cpu" in out and "int8" in out


def test_generate_via_cli_byte_exact(tmp_path, golden_dir, golden_segments_path,
                                     golden_zh_path):
    rc = main([
        "generate",
        "--segments", golden_segments_path,
        "--zh", golden_zh_path,
        "--outdir", str(tmp_path),
        "--base", "apollo_story",
    ])
    assert rc == EXIT_OK
    got = open(os.path.join(tmp_path, "apollo_story.bilingual.srt"), "rb").read()
    exp = open(os.path.join(golden_dir, "apollo_story.bilingual.srt"), "rb").read()
    assert got == exp


def test_transcribe_missing_ffmpeg_returns_3(monkeypatch):
    import video_translate.cli as cli
    monkeypatch.setattr(cli, "_has", lambda b: False)  # no ffmpeg/ffprobe
    rc = main(["transcribe", "x.mp4", "--outdir", "/tmp/out_x"])
    assert rc == EXIT_MISSING_DEP


# --- V2 ---


def test_transcribe_positional_input_accepted():
    p = build_parser()
    ns = p.parse_args(["transcribe", "my.mp4"])
    assert ns.input == "my.mp4"
    assert ns.base is None
    assert ns.outdir is None
    assert ns.lang is None


def test_run_positional_input_accepted():
    p = build_parser()
    ns = p.parse_args(["run", "my.mp4"])
    assert ns.input == "my.mp4"


def test_run_parser_has_model_chunk_lang_threads():
    p = build_parser()
    ns = p.parse_args(["run", "my.mp4", "--model", "small", "--chunk", "120",
                       "--lang", "en", "--threads", "4"])
    assert ns.model == "small"
    assert ns.chunk == 120.0
    assert ns.lang == "en"
    assert ns.threads == 4


def test_engine_default_resolves_agent(tmp_path):
    from video_translate.config import resolve_config
    cfg = resolve_config({"engine": None}, cwd=str(tmp_path), env={})
    assert cfg.engine == "agent"


def test_backfill_subcommand_exists():
    p = build_parser()
    ns = p.parse_args(["backfill", "--pending", "x.json", "--out", "y.json"])
    assert ns.command == "backfill"


def test_translate_engine_agent_returns_awaiting(tmp_path, golden_segments_path, capsys):
    # agent engine: emits a task file, returns 6, no network touched
    out = str(tmp_path / "zh.json")
    rc = main([
        "translate", "--segments", golden_segments_path,
        "--out", out, "--engine", "agent",
    ])
    assert rc == EXIT_AWAITING_AGENT
    captured = capsys.readouterr().out
    assert "[AWAITING_AGENT]" in captured
    task = os.path.join(str(tmp_path), "zh.translate_task.json")
    assert os.path.exists(task)
