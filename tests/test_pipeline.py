"""Control-plane Step 7 — declarative pipeline + engine + `status` contract.

Covers:
  - the STAGES table shape (order matches state.STAGE_ORDER, exactly one stop
    point = translate, gates referenced are registered);
  - position resolution from artifacts alone (legacy dirs, no state file);
  - state-only signals (verify ok -> done, pending_agent);
  - enforce() raising GateFail with guidance;
  - `video-translate status` human + --json outputs, base auto-discovery.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_translate import cli, pipeline, state as vt_state
from video_translate.capabilities import GateFail
from video_translate.pipeline_def import STAGES


# --------------------------- the declarative table -------------------------

def test_stage_order_matches_state():
    assert tuple(s["id"] for s in STAGES) == vt_state.STAGE_ORDER


def test_exactly_one_stop_point_and_it_is_translate():
    stops = [s for s in STAGES if s["stop_point"]]
    assert [s["id"] for s in stops] == ["translate"]


def test_every_referenced_gate_is_registered():
    for s in STAGES:
        if s["gate"]:
            assert s["gate"] in pipeline.GATES


def test_every_stage_has_title_and_cli():
    for s in STAGES:
        assert s["title"] and s["cli"]


# --------------------------- fixtures ---------------------------------------

SEG = [{"index": 0, "start": 0.0, "end": 1.0, "text": "Hello there.",
        "words": [{"start": 0.0, "end": 1.0, "word": "Hello there."}]}]
ZH = {"0": "你好呀。"}


@pytest.fixture()
def art(tmp_path: Path) -> Path:
    return tmp_path


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
    (wd / f"{base}.bilingual.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n",
                                             encoding="utf-8")


def _ok_caps(name: str) -> bool:
    return True


# --------------------------- position resolution ---------------------------

def test_position_transcribe_when_only_video(art):
    ctx = pipeline.build_ctx(art, "demo", video="demo.mp4")
    pos = pipeline.resolve_position(ctx)
    assert pos["current_stage"] == "transcribe"
    assert pos["next_action"]["stage"] == "transcribe"
    assert not pos["pending_agent"]


def test_position_translate_is_stop_point_pending_agent(art):
    _seg_file(art)
    pos = pipeline.resolve_position(pipeline.build_ctx(art, "demo"))
    assert pos["current_stage"] == "translate"
    assert pos["pending_agent"]
    assert pos["next_action"]["stop_point"] is True


def test_position_generate_after_zh(art):
    _seg_file(art)
    _zh_file(art)
    pos = pipeline.resolve_position(pipeline.build_ctx(art, "demo"))
    assert pos["current_stage"] == "generate"
    assert not pos["pending_agent"]


def test_position_verify_after_srt_and_done_after_state_ok(art):
    _seg_file(art)
    _zh_file(art)
    _srt_file(art)
    ctx = pipeline.build_ctx(art, "demo")
    pos = pipeline.resolve_position(ctx)
    assert pos["current_stage"] == "verify"
    # state says verify passed -> whole pipeline done
    st = vt_state.ensure_state(art, "demo")
    vt_state.record_stage(st, "verify", status="ok")
    pos2 = pipeline.resolve_position(ctx, st)
    assert pos2["done"] and pos2["current_stage"] is None
    assert pos2["next_action"] is None


def test_position_pending_agent_survives_via_state(art):
    _seg_file(art)
    _zh_file(art)
    _srt_file(art)
    st = vt_state.ensure_state(art, "demo")
    vt_state.record_stage(st, "verify", status="pending_agent")
    pos = pipeline.resolve_position(pipeline.build_ctx(art, "demo"), st)
    assert pos["pending_agent"]           # semantic reread owed


def test_render_next_json_is_parseable(art):
    _seg_file(art)
    pos = pipeline.resolve_position(pipeline.build_ctx(art, "demo"))
    blob = json.loads(pipeline.render_next(pos, as_json=True))
    assert blob["current_stage"] == "translate"
    assert blob["next_action"]["stage"] == "translate"


# --------------------------- enforce / gates -------------------------------

def test_enforce_raises_gatefail_when_zh_missing(art):
    _seg_file(art)
    with pytest.raises(GateFail) as ei:
        pipeline.enforce("generate", pipeline.build_ctx(art, "demo"),
                         caps_probe=_ok_caps)
    assert "zh" in ei.value.guidance and "translate" in ei.value.guidance


def test_enforce_reports_missing_capability(art):
    _seg_file(art)
    _zh_file(art)
    with pytest.raises(GateFail) as ei:
        pipeline.enforce("generate", pipeline.build_ctx(art, "demo"),
                         caps_probe=lambda n: False)
    assert "ffmpeg" in ei.value.guidance


def test_gate_flags_incomplete_coverage(art):
    _seg_file(art)
    wd = art / "demo"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "demo.zh_segments.json").write_text(json.dumps({}), encoding="utf-8")
    problems = pipeline.check_stage("generate", pipeline.build_ctx(art, "demo"),
                                    caps_probe=_ok_caps)
    assert any("coverage" in p for p in problems)


def test_gate_flags_count_mismatch(art):
    seg2 = SEG + [{"index": 1, "start": 1.4, "end": 2.4, "text": "More.",
                   "words": [{"start": 1.4, "end": 2.4, "word": "More."}]}]
    wd = art / "demo"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "demo.segments_en.json").write_text(json.dumps(seg2),
                                              encoding="utf-8")
    _zh_file(art)  # only 1 zh for 2 en
    problems = pipeline.check_stage("generate", pipeline.build_ctx(art, "demo"),
                                    caps_probe=_ok_caps)
    assert any("count mismatch" in p for p in problems)


# --------------------------- `status` command ------------------------------

def test_status_no_artifacts_points_to_start(tmp_path, capsys):
    rc = cli.main(["status", "--outdir", str(tmp_path)])
    assert rc == 0
    assert "run" in capsys.readouterr().out


def test_status_discovery_and_stop_point(tmp_path, capsys):
    _seg_file(tmp_path)
    rc = cli.main(["status", "--outdir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stage=translate" in out and "STOP POINT" in out


def test_status_json_parseable(tmp_path, capsys):
    _seg_file(tmp_path)
    rc = cli.main(["status", "--outdir", str(tmp_path), "--json"])
    assert rc == 0
    blob = json.loads(capsys.readouterr().out)
    assert blob["current_stage"] == "translate"
    assert blob["next_action"]["pending_agent"] is True


def test_status_explicit_base(tmp_path, capsys):
    _seg_file(tmp_path, "demo")
    _seg_file(tmp_path, "older")
    rc = cli.main(["status", "--outdir", str(tmp_path), "--base", "older"])
    assert rc == 0
    assert "stage=translate" in capsys.readouterr().out


# ------------------- ADR-038 D7: 引擎特定 caps 由 Provider 自报注入 ----------


def test_transcribe_stage_declares_only_common_caps():
    """阶段表只声明**通用**前置 —— 引擎特定前置由 Provider 自报注入；否则换
    ASR 引擎就得改这张表（D7 要杜绝的正是这件事）。"""
    spec = next(s for s in STAGES if s["id"] == "transcribe")
    assert spec["caps"] == ["ffmpeg", "ffprobe"]


def test_build_ctx_injects_provider_prerequisites(art):
    """ctx 带 `_engine_caps` = Provider 自报里的**硬前置**（core 子集）。

    完整自报集合见 `asr.engine_prerequisites()`（doctor 用未过滤的那份）；
    这里只注入会被闸门当作「阻断项」的硬前置（模型），可选能力见下一条测试。
    """
    from video_translate.asr import FasterWhisperProvider

    prereqs = FasterWhisperProvider().prerequisites()
    ctx = pipeline.build_ctx(art, "demo")
    assert "model:large-v3" in ctx["_engine_caps"]
    assert set(ctx["_engine_caps"]) <= set(prereqs)


def test_build_ctx_accepts_provider_override(art):
    """换引擎只需换 Provider：注入内容随其声明而变，阶段表不动。"""

    class FakeProvider:
        def prerequisites(self):
            return ("model:fake-asr",)

    ctx = pipeline.build_ctx(art, "demo", provider=FakeProvider())
    assert ctx["_engine_caps"] == ("model:fake-asr",)


def test_check_stage_checks_injected_engine_caps(art):
    """注入的引擎前置**参与**检查：缺 model:large-v3 → problem（且带修复指引）。"""
    _seg_file(art)
    ctx = pipeline.build_ctx(art, "demo", video="demo.mp4")
    problems = pipeline.check_stage(
        "transcribe", ctx, caps_probe=lambda n: n in ("ffmpeg", "ffprobe"))
    assert any("model:large-v3" in p for p in problems)
    assert any("setup" in p for p in problems)  # guidance 来自 capabilities


def test_engine_prereq_failure_degrades_to_empty(art):
    """Provider 自报抛错 → 降级为空元组：就绪声明失败不该让 ctx / 位置解析崩掉。"""

    class BoomProvider:
        def prerequisites(self):
            raise RuntimeError("boom")

    ctx = pipeline.build_ctx(art, "demo", provider=BoomProvider())
    assert ctx["_engine_caps"] == ()
    problems = pipeline.check_stage("transcribe", ctx, caps_probe=lambda n: True)
    assert not any("model:" in p for p in problems)


def test_optional_engine_caps_never_block_the_stage(art):
    """可选能力（cuda / whisperx / demucs）缺失**不**阻断 transcribe。

    它们对主链是可选（CPU 可跑、--align auto 降级、demucs 仅 --separate-vocals
    需要），若被当成阻断项，CPU / macOS 机器的 NEXT 块会长期挂误导性 MISS。
    只有硬前置（模型）进 `_engine_caps`；可选能力的体检归 doctor。
    """
    _seg_file(art)
    ctx = pipeline.build_ctx(art, "demo", video="demo.mp4")
    assert "model:large-v3" in ctx["_engine_caps"]
    for optional in ("cuda", "whisperx", "demucs"):
        assert optional not in ctx["_engine_caps"]
    # 即便全部探测失败，也不该出现「可选能力缺失」的阻断项
    problems = pipeline.check_stage("transcribe", ctx, caps_probe=lambda n: False)
    assert any("model:large-v3" in p for p in problems)
    assert not any(
        cap in p for p in problems for cap in ("cuda", "whisperx", "demucs"))
