"""T8 / ADR-033 / Spec 24 — `pipeline` idempotent advancer contract.

Covers:
  - ``pipeline.next_action``: the six-action decision table across the three
    ``--prompt`` modes (pure function, zero I/O);
  - ``Config.prompt``: default ``always`` + env/toml/CLI chain + invalid-value
    fallback (same idiom as VT_ALIGN);
  - ``cmd_pipeline`` dispatch: fresh base stops at the decision point (exit 6),
    ``--prompt never`` delegates to cmd_run, generate/verify delegate to their
    executors with derived paths, done prints completion (exit 0);
  - ``--prompt require-profile`` without explicit routing -> exit 8 (reuses the
    ``run --require-profile`` hard gate);
  - explicit ``--style`` forwarding into the delegated run namespace.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_translate import cli, config, pipeline, state as vt_state

# --------------------------- fixtures ---------------------------------------

SEG = [{"index": 0, "start": 0.0, "end": 1.0, "text": "Hello there.",
        "words": [{"start": 0.0, "end": 1.0, "word": "Hello there."}]}]
ZH = {"0": "你好呀。"}


def _seg_file(d: Path, base: str = "demo") -> None:
    wd = d / base
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{base}.segments_en.json").write_text(json.dumps(SEG),
                                                encoding="utf-8")


def _zh_file(d: Path, base: str = "demo") -> None:
    wd = d / base
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{base}.zh_segments.json").write_text(json.dumps(ZH),
                                                encoding="utf-8")


def _srt_file(d: Path, base: str = "demo") -> None:
    wd = d / base
    wd.mkdir(parents=True, exist_ok=True)
    (wd / f"{base}.bilingual.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")


# --------------------------- next_action decision table ---------------------

def _pos(stage: str | None, *, done: bool = False,
         pending_agent: bool = False) -> dict:
    return {"current_stage": stage, "done": done,
            "pending_agent": pending_agent}


ROUTING_EXPLICIT = {"origin": "explicit", "style": "film"}
ROUTING_PROFILE = {"origin": "profile", "style": "film"}


def test_done_position_wins_over_everything():
    for mode in ("always", "never", "require-profile"):
        assert pipeline.next_action(_pos(None, done=True),
                                    prompt_mode=mode, routing=None) == "done"


def test_fresh_base_always_stops_at_decision_point():
    assert pipeline.next_action(_pos("transcribe"), prompt_mode="always",
                                routing=None) == "stop_decision_point"


@pytest.mark.parametrize("routing", [ROUTING_EXPLICIT, ROUTING_PROFILE])
def test_existing_routing_means_no_second_decision_stop(routing):
    # T8: 重跑 pipeline 见 routing 已存在即续 transcribe（任意 origin）。
    assert pipeline.next_action(_pos("transcribe"), prompt_mode="always",
                                routing=routing) == "transcribe"


@pytest.mark.parametrize("mode", ["never", "require-profile"])
def test_non_always_modes_never_stop_at_decision_point(mode):
    assert pipeline.next_action(_pos("transcribe"), prompt_mode=mode,
                                routing=None) == "transcribe"


def test_translate_position_is_agent_stop():
    for mode in ("always", "never", "require-profile"):
        assert pipeline.next_action(_pos("translate"), prompt_mode=mode,
                                    routing=None) == "stop_translate"


def test_generate_and_verify_positions_delegate():
    assert pipeline.next_action(_pos("generate"), prompt_mode="always",
                                routing=None) == "generate"
    assert pipeline.next_action(_pos("verify"), prompt_mode="never",
                                routing=None) == "verify"


def test_next_action_reads_nothing_but_position_fields():
    # 纯函数自证：残缺 pos 不崩（防御未知字段增删）。
    # 空 pos ≈ 全新 base：always + 无 routing 时正确落到决策点停点。
    assert pipeline.next_action({}, prompt_mode="always",
                                routing=None) == "stop_decision_point"
    assert pipeline.next_action({}, prompt_mode="never",
                                routing=None) == "transcribe"


# --------------------------- Config.prompt ----------------------------------

def test_prompt_defaults_to_always(tmp_path):
    cfg = config.resolve_config({}, cwd=str(tmp_path), env={})
    assert cfg.prompt == "always"


def test_prompt_env_chain(tmp_path):
    cfg = config.resolve_config({}, cwd=str(tmp_path),
                                env={"VT_PROMPT": "never"})
    assert cfg.prompt == "never"
    assert cfg._sources["prompt"] == "env"


def test_prompt_env_invalid_falls_back_to_always(tmp_path, capsys):
    cfg = config.resolve_config({}, cwd=str(tmp_path),
                                env={"VT_PROMPT": "bogus"})
    assert cfg.prompt == "always"
    assert "VT_PROMPT" in capsys.readouterr().err


def test_prompt_cli_override_and_invalid_fallback(tmp_path, capsys):
    cfg = config.resolve_config({"prompt": "require-profile"},
                                cwd=str(tmp_path), env={})
    assert cfg.prompt == "require-profile"
    bad = config.resolve_config({"prompt": "bogus"}, cwd=str(tmp_path), env={})
    assert bad.prompt == "always"
    assert "--prompt" in capsys.readouterr().err


def test_prompt_toml_section(tmp_path):
    (tmp_path / config.CONFIG_FILENAME).write_text(
        '[pipeline]\nprompt = "never"\n', encoding="utf-8")
    cfg = config.resolve_config({}, cwd=str(tmp_path), env={})
    assert cfg.prompt == "never"


# --------------------------- subcommand surface -----------------------------

def test_pipeline_subcommand_registered():
    ns = cli.build_parser().parse_args(["pipeline", "x.mp4"])
    assert ns.func is cli.cmd_pipeline
    assert ns.prompt is None  # None -> config default (always)


def test_pipeline_prompt_choices_validated():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["pipeline", "x.mp4", "--prompt", "bogus"])


# --------------------------- cmd_pipeline dispatch --------------------------

def test_dispatch_decision_point_stop(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ensure_audio_profile",
                        lambda *a, **k: None)
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo"])
    assert rc == 6  # EXIT_AWAITING_AGENT
    out = capsys.readouterr().out
    assert "STOP POINT" in out and "decision point" in out
    assert "--style" in out  # tells the agent how to persist the choice


def test_dispatch_never_delegates_to_run(tmp_path, monkeypatch, capsys):
    seen: list = []

    def fake_run(ns):
        seen.append(ns)
        return 6

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo", "--prompt", "never"])
    assert rc == 6
    assert seen and seen[0].input == "v.mp4"
    assert seen[0].outdir == str(tmp_path)


def test_dispatch_forwards_explicit_style(tmp_path, monkeypatch, capsys):
    seen: list = []

    def fake_run(ns):
        seen.append(ns)
        return 6

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path), "--base", "demo",
              "--prompt", "never", "--style", "literal"])
    assert seen[0].style == "literal"


def test_explicit_style_satisfies_decision_point(tmp_path, monkeypatch, capsys):
    # The user's decision-point answer arrives as a re-run with --style:
    # routing is still None on disk, but an explicit flag means "already
    # decided" — pipeline must proceed to transcribe, not re-stop.
    seen: list = []

    def fake_run(ns):
        seen.append(ns)
        return 6

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo", "--style", "literal"])  # default prompt
    assert rc == 6
    assert seen and seen[0].style == "literal"


def test_dispatch_generate(tmp_path, monkeypatch, capsys):
    _seg_file(tmp_path)
    _zh_file(tmp_path)
    seen: list = []

    def fake_generate(ns):
        seen.append(ns)
        return 0

    monkeypatch.setattr(cli, "cmd_generate", fake_generate)
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo"])
    assert rc == 0
    assert seen[0].segments.endswith("demo.segments_en.json")
    assert seen[0].zh.endswith("demo.zh_segments.json")
    # executor defaults must survive delegation (gap 0.2 / min-dur 1.0 / tail 0.3)
    assert seen[0].gap == 0.2 and seen[0].min_dur == 1.0 and seen[0].tail == 0.3


def test_dispatch_verify(tmp_path, monkeypatch, capsys):
    _seg_file(tmp_path)
    _zh_file(tmp_path)
    _srt_file(tmp_path)
    seen: list = []

    def fake_verify(ns):
        seen.append(ns)
        return 0

    monkeypatch.setattr(cli, "cmd_verify", fake_verify)
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo"])
    assert rc == 0
    assert seen[0].video == "v.mp4"
    assert seen[0].zh.endswith("demo.zh_segments.json")


def test_dispatch_translate_stop_point(tmp_path, monkeypatch, capsys):
    _seg_file(tmp_path)
    wd = tmp_path / "demo"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "demo.translate_task.json").write_text("{}", encoding="utf-8")
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo"])
    assert rc == 6
    out = capsys.readouterr().out
    assert "translate_task" in out and "zh_segments" in out


def test_dispatch_translate_without_task_self_heals_via_run(
        tmp_path, monkeypatch, capsys):
    # segments exist but the task was lost (interrupted run): pipeline must
    # regenerate it via `run --skip transcribe`, not print a dangling pointer.
    _seg_file(tmp_path)
    seen: list = []

    def fake_run(ns):
        seen.append(ns)
        return 6

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo"])
    assert rc == 6
    assert seen and list(seen[0].skip) == ["transcribe"]


def test_dispatch_done_exit_zero(tmp_path, capsys):
    _seg_file(tmp_path)
    _zh_file(tmp_path)
    _srt_file(tmp_path)
    st = vt_state.ensure_state(tmp_path, "demo")
    vt_state.record_stage(st, "verify", status="ok")
    vt_state.save(tmp_path, "demo", st)
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo"])
    assert rc == 0
    assert "complete" in capsys.readouterr().out


def test_dispatch_require_profile_without_explicit_is_gate_fail(
        tmp_path, capsys):
    rc = cli.main(["pipeline", "v.mp4", "--outdir", str(tmp_path),
                   "--base", "demo", "--prompt", "require-profile"])
    assert rc == 8
    assert "require-profile" in capsys.readouterr().out
