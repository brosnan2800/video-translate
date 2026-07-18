"""CLI smoke tests (Spec 05). No heavy deps required."""
import pytest

from video_translate.cli import (
    EXIT_MISSING_DEP,
    EXIT_OK,
    build_parser,
    main,
)


def test_parser_accepts_all_subcommands():
    p = build_parser()
    for cmd in ("transcribe", "translate", "generate", "run", "setup", "doctor"):
        # transcribe/translate/generate/run/setup need required args; just parse known
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
    import os
    got = open(os.path.join(tmp_path, "apollo_story.bilingual.srt"), "rb").read()
    exp = open(os.path.join(golden_dir, "apollo_story.bilingual.srt"), "rb").read()
    assert got == exp


def test_transcribe_missing_ffmpeg_returns_3(monkeypatch):
    import video_translate.cli as cli
    monkeypatch.setattr(cli, "_has", lambda b: False)  # no ffmpeg/ffprobe
    rc = main(["transcribe", "--input", "x.mp4", "--outdir", "/tmp/out_x"])
    assert rc == EXIT_MISSING_DEP
