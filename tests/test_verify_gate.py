"""Control-plane Step 6 — verify 收口 contract tests (§2.1 静默点 5-9).

Covers the hard-gate semantics of ``cmd_verify``:
  - 静默点 5: strict is the DEFAULT (any lane flag -> exit 8);
    ``--no-strict`` is the explicit escape hatch (report-only, exit 0);
    legacy ``--strict`` stays a no-op.
  - Missing --zh / --video -> exit 2 (verify must run EVERY lane).
  - 静默点 7: audio-profile failure -> RED acoustic lane (exit 8), never skip.
  - 静默点 8: uncovered-probe exception -> RED lane + reason, never [].
  - 静默点 9: reread result consumption (non-ok verdicts -> exit 8;
    all-ok -> pass; missing result -> pending_agent state + task hung).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from video_translate import cli


# --------------------------- fixtures / helpers ---------------------------

def _seg(i: int, text: str, start: float, end: float) -> dict:
    w = [{"start": start, "end": end, "word": t}
         for t in text.split()] or [{"start": start, "end": end, "word": text}]
    return {"index": i, "start": start, "end": end, "text": text, "words": w}


EN = [
    _seg(0, "Hello there.", 0.0, 1.0),
    _seg(1, "General Kenobi.", 1.4, 2.4),
    _seg(2, "You are a bold one.", 2.8, 4.0),
]
ZH = {"0": "你好呀。", "1": "欧比旺·克诺比。", "2": "你是个勇敢的家伙。"}


@pytest.fixture()
def art(tmp_path: Path) -> Path:
    """Artifacts dir with en segments + full zh translation (in workdir)."""
    wd = tmp_path / "demo"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "demo.segments_en.json").write_text(
        json.dumps(EN), encoding="utf-8")
    (wd / "demo.zh_segments.json").write_text(
        json.dumps(ZH), encoding="utf-8")
    # display-window sidecar (as `generate` writes it) so the presentation
    # lane sees the healthy defaults instead of stripped 0/0.
    (wd / "demo.generate_opts.json").write_text(
        json.dumps({"tail": 0.3, "min_dur": 1.0, "offset": 0.0,
                    "gap": 0.08, "style": None}), encoding="utf-8")
    return wd


def _fake_video(tmp_path: Path) -> str:
    p = tmp_path / "demo.mp4"
    p.write_bytes(b"")
    return str(p)


def _run(art: Path, extra: list[str]) -> int:
    ns = cli.build_parser().parse_args(
        ["verify", "--segments", str(art / "demo.segments_en.json"),
         "--zh", str(art / "demo.zh_segments.json"),
         "--video", _fake_video(art), *extra])
    return cli.cmd_verify(ns)


@pytest.fixture()
def no_probe(monkeypatch):
    """Analyze-audio returns a healthy profile with two silence gaps."""

    def _ok(video, noise="-30dB", d=0.3):
        from video_translate.audio_profile import AudioProfile
        return AudioProfile(mean_vol=-18.0, max_vol=-3.0,
                            silence_intervals=[(1.0, 1.4), (2.4, 2.8)],
                            duration=5.0, ok=True)

    monkeypatch.setattr(cli, "analyze_audio", _ok)


# --------------------------- 静默点 5: strict default ---------------------

def test_strict_is_default_red_lane_exits_8(art, no_probe, monkeypatch):
    """Bogus zh (index drift) + default strict -> EXIT_GATE_FAIL."""
    (art / "demo.zh_segments.json").write_text(
        json.dumps({"0": "你好呀。"}), encoding="utf-8")  # coverage 1/3
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    assert _run(art, []) == cli.EXIT_GATE_FAIL


def test_no_strict_is_report_only(art, no_probe, monkeypatch):
    """Same red input with --no-strict -> exit 0 (explicit escape hatch)."""
    (art / "demo.zh_segments.json").write_text(
        json.dumps({"0": "你好呀。"}), encoding="utf-8")
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    assert _run(art, ["--no-strict"]) == cli.EXIT_OK


def test_legacy_strict_flag_still_accepted(art, no_probe, monkeypatch):
    """Legacy --strict parses (no-op) and a clean run still exits 0."""
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    assert _run(art, ["--strict"]) == cli.EXIT_OK


def test_clean_run_exits_0(art, no_probe, monkeypatch):
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    assert _run(art, []) == cli.EXIT_OK


# --------------------------- missing args -> exit 2 -----------------------

def test_missing_zh_refuses_to_run(art):
    ns = cli.build_parser().parse_args(
        ["verify", "--segments", str(art / "demo.segments_en.json"),
         "--video", _fake_video(art)])
    assert cli.cmd_verify(ns) == cli.EXIT_ARGS


def test_missing_video_refuses_to_run(art):
    ns = cli.build_parser().parse_args(
        ["verify", "--segments", str(art / "demo.segments_en.json"),
         "--zh", str(art / "demo.zh_segments.json")])
    assert cli.cmd_verify(ns) == cli.EXIT_ARGS



# --------------------------- 静默点 7: profile failure --------------------

def test_profile_failure_is_red_not_skip(art, monkeypatch):
    def _dead(video, noise="-30dB", d=0.3):
        from video_translate.audio_profile import AudioProfile
        return AudioProfile(ok=False)

    monkeypatch.setattr(cli, "analyze_audio", _dead)
    assert _run(art, []) == cli.EXIT_GATE_FAIL


def test_profile_probe_exception_is_red(art, monkeypatch):
    def _boom(video, noise="-30dB", d=0.3):
        raise RuntimeError("ffmpeg vanished")

    monkeypatch.setattr(cli, "analyze_audio", _boom)
    assert _run(art, []) == cli.EXIT_GATE_FAIL


# --------------------------- 静默点 8: uncovered probe --------------------

def test_uncovered_probe_exception_is_red(art, no_probe, monkeypatch):
    def _boom(video):
        raise RuntimeError("ffprobe exploded")

    monkeypatch.setattr(cli, "probe_duration", _boom)
    assert _run(art, []) == cli.EXIT_GATE_FAIL


def test_uncovered_windows_are_red(art, no_probe, monkeypatch):
    """Cues end at 4.0s, audio runs to 6.5s -> 2.5s uncovered tail -> exit 8."""
    monkeypatch.setattr(cli, "probe_duration", lambda v: 6.5)
    assert _run(art, []) == cli.EXIT_GATE_FAIL


# --------------------------- 静默点 9: reread consumption -----------------

def test_reread_result_flags_fail_gate(art, no_probe, monkeypatch):
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    (art / "demo.semantic_reread_result.json").write_text(
        json.dumps({"1": "wrong: dropped the second clause"}),
        encoding="utf-8")
    assert _run(art, []) == cli.EXIT_GATE_FAIL


def test_reread_result_all_ok_passes(art, no_probe, monkeypatch):
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    (art / "demo.semantic_reread_result.json").write_text(
        json.dumps({"0": "ok", "1": "ok", "2": "ok"}), encoding="utf-8")
    assert _run(art, []) == cli.EXIT_OK


def test_reread_result_missing_hangs_task_and_marks_pending(
        art, no_probe, monkeypatch):
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    assert _run(art, ["--no-semantic"]) == cli.EXIT_OK  # hint, not a gate
    task = art / "demo.semantic_reread_task.json"
    assert task.exists()                      # task (re)hung
    st = json.loads((art / "demo.vt_state.json").read_text(encoding="utf-8"))
    assert st["stages"]["verify"]["status"] == "pending_agent"


def test_state_hook_survives_corrupt_state(art, no_probe, monkeypatch):
    """Corrupt vt_state.json must not crash verify (state is never a gate)."""
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    (art / "demo.vt_state.json").write_text("{not json", encoding="utf-8")
    assert _run(art, ["--no-semantic"]) == cli.EXIT_OK


# --------------------------- ADR-031: 声学 lane 硬化 ------------------------

def test_low_confidence_segments_turn_acoustic_lane_red(art, no_probe, monkeypatch):
    """Whisper 自判非语音的段（nsp>=0.6）必须让声学 lane 红（D3）。"""
    segs = json.loads((art / "demo.segments_en.json").read_text(encoding="utf-8"))
    segs[1]["no_speech_prob"] = 0.906
    (art / "demo.segments_en.json").write_text(
        json.dumps(segs), encoding="utf-8")
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    assert _run(art, []) == cli.EXIT_GATE_FAIL


def test_adjacent_overlap_turns_acoustic_lane_red(art, no_probe, monkeypatch):
    """相邻段声学窗口重叠 >0.05s -> 声学 lane 红（D4）。"""
    segs = json.loads((art / "demo.segments_en.json").read_text(encoding="utf-8"))
    segs[1]["start"] = 0.9                       # 骑在 seg0 [0.0,1.0] 上，重叠 0.1s
    segs[1]["words"][0]["start"] = 0.9
    (art / "demo.segments_en.json").write_text(
        json.dumps(segs), encoding="utf-8")
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    assert _run(art, []) == cli.EXIT_GATE_FAIL


def test_uncovered_windows_classified_by_vocals_energy(
        art, no_probe, monkeypatch, capsys):
    """uncovered 窗自动按人声轨能量分级（D7）：BGM 窗给「无需 resegment」建议。"""
    (art / "demo.7a62b360.vocals.wav").write_bytes(b"")
    monkeypatch.setattr(cli, "probe_duration", lambda v: 6.5)   # uncovered (4.0, 6.5)
    monkeypatch.setattr(cli, "probe_volume_window",
                        lambda path, s, e, ff=None: (-40.0, -22.0))
    rc = _run(art, [])
    assert rc == cli.EXIT_GATE_FAIL              # uncovered 本身仍是红
    out = capsys.readouterr().out
    assert "[bgm]" in out
    assert "likely BGM" in out


def test_uncovered_classification_skipped_without_vocals(
        art, no_probe, monkeypatch, capsys):
    """无 vocals 缓存时跳过分级（行为与 ADR-031 之前一致）。"""
    monkeypatch.setattr(cli, "probe_duration", lambda v: 6.5)
    assert _run(art, []) == cli.EXIT_GATE_FAIL
    out = capsys.readouterr().out
    assert "[bgm]" not in out


def test_reread_task_carries_recovered_suspect_hints(art, no_probe, monkeypatch):
    """恢复段 pair 附 suspect+hint（D2）——语义回读不能只靠语义合理性放行。"""
    segs = json.loads((art / "demo.segments_en.json").read_text(encoding="utf-8"))
    segs[1]["_recovered"] = True
    (art / "demo.segments_en.json").write_text(
        json.dumps(segs), encoding="utf-8")
    monkeypatch.setattr(cli, "probe_duration", lambda v: 5.0)
    _run(art, [])                                # semantic 默认开启 -> task 落盘
    task = json.loads(
        (art / "demo.semantic_reread_task.json").read_text(encoding="utf-8"))
    pairs = {p["index"]: p for p in task["pairs"]}
    assert pairs[1]["suspect"] is True
    assert "hallucination" in pairs[1]["hint"]
    assert "suspect" not in pairs[0]
