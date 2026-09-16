"""Tests for the doctor command's Google-endpoint reachability probe (Spec 11).

The probe must NOT touch the network in unit tests — we monkeypatch the probe and
every environment touch so the check is fully deterministic.
"""
import argparse

from video_translate import cli
from video_translate.config import Config


def _args(**kw):
    base = dict(strict=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _patch_all(monkeypatch, reachable):
    monkeypatch.setattr(cli, "_has", lambda b: True)
    # model cache helpers moved cli → model_cache (ADR-038 D7 / 循环依赖收敛)；
    # doctor 从 cli 命名空间调用它们，故 patch cli 侧的绑定。
    monkeypatch.setattr(cli, "hf_cache_dir", lambda: "/tmp/cache")
    monkeypatch.setattr(cli, "model_cached", lambda m: True)
    # Provider 自报的 model:* 体检项经 capabilities.probe（ADR-038 D7）——
    # 抹平本机是否真的放了模型，保证断言确定性。
    monkeypatch.setattr("video_translate.capabilities.probe", lambda n: True)
    monkeypatch.setattr(cli, "_cuda_available", lambda: False)
    monkeypatch.setattr(cli, "resolve_config", lambda **k: Config())
    monkeypatch.setattr(cli, "detect_proxy", lambda **k: None)
    # imported inside cmd_doctor via `from .proxy import _probe_google_endpoint`
    monkeypatch.setattr("video_translate.proxy._probe_google_endpoint",
                        lambda p: reachable)


def test_doctor_google_reachable_returns_ok(monkeypatch):
    _patch_all(monkeypatch, reachable=True)
    assert cli.cmd_doctor(_args()) == cli.EXIT_OK


def test_doctor_google_unreachable_still_ok_by_default(monkeypatch):
    """Default doctor never hard-fails: a MISS is printed but exit code is 0."""
    _patch_all(monkeypatch, reachable=False)
    assert cli.cmd_doctor(_args()) == cli.EXIT_OK


def test_doctor_strict_unreachable_returns_7(monkeypatch, capsys):
    _patch_all(monkeypatch, reachable=False)
    rc = cli.cmd_doctor(_args(strict=True))
    assert rc == cli.EXIT_DOCTOR_FAIL
    out = capsys.readouterr().out
    assert "MISS" in out  # the Google endpoint line prints MISS


def _has_for(ffmpeg_present: bool):
    """_has() that reports ffmpeg/ffprobe per `ffmpeg_present`, everything else OK."""
    def _has(binary: str) -> bool:
        if binary in ("ffmpeg", "ffprobe"):
            return ffmpeg_present
        return True
    return _has


def test_doctor_ffmpeg_missing_default_returns_7(monkeypatch, capsys):
    """ffmpeg/ffprobe is a hard dep of the core pipeline: missing by default
    hard-fails (exit 7) WITHOUT --strict, even when all optional deps are fine."""
    _patch_all(monkeypatch, reachable=True)  # Google/optional deps OK
    monkeypatch.setattr(cli, "_has", _has_for(False))
    rc = cli.cmd_doctor(_args())
    assert rc == cli.EXIT_DOCTOR_FAIL
    out = capsys.readouterr().out
    assert "GATE" in out
    assert "setup --ffmpeg" in out  # deterministic fix still surfaced


def test_doctor_ffprobe_missing_default_returns_7(monkeypatch, capsys):
    """ffprobe alone missing is also a hard-fail (both binaries are hard deps)."""
    _patch_all(monkeypatch, reachable=True)
    monkeypatch.setattr(cli, "_has", lambda b: b != "ffprobe")
    assert cli.cmd_doctor(_args()) == cli.EXIT_DOCTOR_FAIL


def test_doctor_ffmpeg_present_optional_dep_missing_still_ok(monkeypatch, capsys):
    """With ffmpeg present, an optional-dep MISS (Google unreachable) stays exit 0
    by default — only --strict would gate it. Confirms the new hard gate scopes to
    ffmpeg/ffprobe only, not optional deps."""
    _patch_all(monkeypatch, reachable=False)
    monkeypatch.setattr(cli, "_has", _has_for(True))
    assert cli.cmd_doctor(_args()) == cli.EXIT_OK

