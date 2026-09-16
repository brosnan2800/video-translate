"""契约测试：控制平面意图分级闸（裁决一）—— explicit 缺能力 → GateFail(exit 8)，
--allow-degrade 显式逃生；default 缺能力 → 降级留痕。

覆盖三个实测静默点：
  2  --separate-vocals 显式 + demucs 缺 → 硬停（原回退原音频）
  3b --align whisperx 显式 + 缺包    → 硬停（原 WARN 降级 none）
含 auto 降级零回归断言。
"""
import argparse

import pytest

from video_translate import asr as A
from video_translate import cli
from video_translate.asr import AsrRequest, TranscriberConfig
from video_translate.capabilities import GateFail


def _ns(**kw):
    base = dict(allow_degrade=False)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# vsep gate (静默点 2: asr._vocal_sep_step —— ADR-038 第二步 A 块后归位 ① 层门面)
# ---------------------------------------------------------------------------

def _req(separate_vocals: bool, *, allow_degrade: bool = False) -> AsrRequest:
    """构造最小 AsrRequest（vsep 路径只关心这两个输入）。"""
    return AsrRequest(
        input_path="in.mp4", outdir="out", base="base",
        config=TranscriberConfig(separate_vocals=separate_vocals),
        allow_degrade=allow_degrade,
    )


def test_vsep_missing_demucs_hard_stops(monkeypatch):
    """显式 --separate-vocals + demucs 缺 -> GateFail（exit 8），绝不静默回退。"""
    monkeypatch.setattr(
        "video_translate.vocal_sep.demucs_available", lambda: False
    )
    with pytest.raises(GateFail) as exc:
        A._vocal_sep_step(_req(separate_vocals=True))
    assert "separate-vocals" in exc.value.message
    assert "uv sync" in exc.value.guidance


def test_vsep_missing_demucs_with_escape_hatch_degrades(monkeypatch):
    """--allow-degrade 逃生门：显式 vsep 缺 demucs → 警告并回退原音频，不硬停。"""
    monkeypatch.setattr(
        "video_translate.vocal_sep.demucs_available", lambda: False
    )
    sep_on, audio_src, *_ = A._vocal_sep_step(
        _req(separate_vocals=True, allow_degrade=True))
    assert sep_on is False  # degraded: no separation actually ran
    assert audio_src is None


def test_vsep_not_requested_stays_off():
    sep_on, audio_src, *_ = A._vocal_sep_step(_req(separate_vocals=False))
    assert sep_on is False and audio_src is None


def test_vsep_resegment_missing_demucs_hard_stops(monkeypatch):
    """静默点 2（resegment 线路）：显式 vsep + demucs 缺 → GateFail。"""
    monkeypatch.setattr(
        "video_translate.vocal_sep.demucs_available", lambda: False
    )
    with pytest.raises(GateFail):
        A._gate_vsep(
            False,
            "--separate-vocals requested on resegment but the demucs "
            "package is not installed.",
            "Run `uv sync` (`pip install -e .` fallback)",
        )


# ---------------------------------------------------------------------------
# align gate (静默点 3b: transcribe._resolve_align_backend)
# ---------------------------------------------------------------------------

def test_align_explicit_whisperx_unavailable_raises(monkeypatch):
    import video_translate.transcribe as T
    import video_translate.align as AL

    monkeypatch.setattr(AL, "whisperx_available", lambda: False)
    with pytest.raises(GateFail) as exc:
        T._resolve_align_backend("whisperx")
    assert "WhisperX" in exc.value.message
    assert "uv sync --extra gpu" in exc.value.guidance


def test_align_explicit_unknown_backend_raises(monkeypatch):
    import video_translate.transcribe as T

    with pytest.raises(GateFail):
        T._resolve_align_backend("hokus-pokus")


def test_align_auto_degrades_silently(monkeypatch, capsys):
    """零回归：auto 缺能力 → 降级 none（info，不硬停），默认路径行为不变。"""
    import video_translate.transcribe as T
    import video_translate.align as AL

    monkeypatch.setattr(AL, "whisperx_available", lambda: False)
    assert T._resolve_align_backend("auto") == "none"


def test_align_none_passthrough():
    import video_translate.transcribe as T

    assert T._resolve_align_backend("none") == "none"


# ---------------------------------------------------------------------------
# cli.main 的 GateFail → exit 8 收口（与 GateFail 类型一体，放这里验证）
# ---------------------------------------------------------------------------

def test_gatefail_flow_through_main_returns_8(monkeypatch):
    """裁决一协议：显式请求缺能力 → main 返回 EXIT_GATE_FAIL(8)，绝不停 6。

    真实走 cmd_transcribe：mock _vocal_sep_step 抛 GateFail（demucs 缺失场景），
    main 捕获 → 8。验证 parser 路径（args.func=cmd_transcribe）下的收口，而非
    直接调 handler。
    """
    from video_translate.capabilities import GateFail as GF

    def _boom(*a, **k):
        raise GF(
            "--separate-vocals requires the demucs package, which is not "
            "installed. Explicit requests are never silently degraded.",
            "Run `uv sync` (`pip install -e .` fallback).",
        )

    # ffmpeg present on host (doctor green); keep hermetic regardless:
    monkeypatch.setattr(cli, "_require_ffmpeg", lambda: None)
    # 分离步骤已下沉 ① 层门面（ADR-038 第二步 A 块）：打在 asr 模块属性上
    monkeypatch.setattr("video_translate.asr._vocal_sep_step", _boom)

    rc = cli.main(["transcribe", "vid.mp4"])
    assert rc == cli.EXIT_GATE_FAIL == 8