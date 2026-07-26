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
    monkeypatch.setattr(cli, "_hf_cache_dir", lambda: "/tmp/cache")
    monkeypatch.setattr(cli, "_model_cached", lambda m: True)
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
