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


def _route(lane="cpu", device=None, can_separate=False,
           message="CPU 模式不支持人声分离（GPU-only）"):
    """Build a VsepRoute (Spec 25) for stubbing the vocal-separation route."""
    from video_translate.vocal_sep import VsepRoute
    return VsepRoute(lane=lane, device=device,
                     can_separate=can_separate, message=message)


def _patch_all(monkeypatch, reachable, route=None, demucs_cached=True):
    monkeypatch.setattr(cli, "_has", lambda b: True)
    monkeypatch.setattr(cli, "_hf_cache_dir", lambda: "/tmp/cache")
    monkeypatch.setattr(cli, "_model_cached", lambda m: True)
    # ADR-032 / Spec 26 — doctor also reports the Demucs model cache now.
    # Default the stub to "cached" so unrelated doctor tests stay isolated from
    # the real filesystem; tests exercising miss/truncated override it.
    monkeypatch.setattr(
        cli, "_demucs_model_cached",
        lambda *a, **k: (demucs_cached,
                         "/tmp/demucs-cache" if demucs_cached else "missing"),
    )
    monkeypatch.setattr(cli, "_cuda_available", lambda: False)
    monkeypatch.setattr(cli, "resolve_config", lambda **k: Config())
    monkeypatch.setattr(cli, "detect_proxy", lambda **k: None)
    # Spec 25: doctor reports the separation lane from the single route source.
    # demucs is pinned available so the three-lane branch is always exercised.
    monkeypatch.setattr("video_translate.vocal_sep.demucs_available",
                        lambda: True)
    monkeypatch.setattr("video_translate.vocal_sep.resolve_vsep_route",
                        lambda: route or _route())
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


class TestDoctorSeparationLane:
    """Spec 25 § 消费者行为 4 — doctor 按 lane 三线展示，而不是"有没有 CUDA"。"""

    def test_cuda_ready_reports_ok(self, monkeypatch, capsys):
        r = _route(lane="cuda", device="cuda", can_separate=True, message="")
        _patch_all(monkeypatch, reachable=True, route=r)
        cli.cmd_doctor(_args())
        assert "OK" in capsys.readouterr().out

    def test_cuda_not_ready_points_at_setup(self, monkeypatch, capsys):
        r = _route(lane="cuda", device=None, can_separate=False,
                   message="检测到 NVIDIA GPU 但 CUDA 未就绪，请运行 make setup")
        _patch_all(monkeypatch, reachable=True, route=r)
        cli.cmd_doctor(_args())
        assert "setup" in capsys.readouterr().out

    def test_apple_silicon_reports_todo(self, monkeypatch, capsys):
        """Apple Silicon 必须显示为待办，而不是被当成"无 GPU 禁用"。"""
        r = _route(lane="apple_silicon", device=None, can_separate=False,
                   message="Apple Silicon 人声分离为待办（TODO），本期未实现")
        _patch_all(monkeypatch, reachable=True, route=r)
        cli.cmd_doctor(_args())
        assert "TODO" in capsys.readouterr().out

    def test_cpu_lane_reports_unsupported(self, monkeypatch, capsys):
        _patch_all(monkeypatch, reachable=True, route=_route())
        cli.cmd_doctor(_args())
        assert "CPU" in capsys.readouterr().out


class TestDoctorDemucsModelCache:
    """ADR-032 Bug 2 / Spec 26 — doctor must report the htdemucs cache truthfully.

    Before ADR-032 the demucs line was derived from a bare `import demucs` probe
    plus the CUDA lane: package present + CUDA ready printed "OK". Weights sitting
    on the C: drive — or missing entirely — were therefore never surfaced. That
    false green light is exactly what let Bug 1 survive unnoticed from the very
    first `--separate-vocals` run.
    """

    @staticmethod
    def _build_cache(tmp_path, *, weights_bytes=None, with_yaml=True):
        """Fake a project-local htdemucs cache tree (demucs 4.x HF layout)."""
        snap = (tmp_path / "hub" / "models--adefossez--HTDemucs"
                / "snapshots" / "bf35a81b")
        snap.mkdir(parents=True, exist_ok=True)
        if with_yaml:
            (snap / "htdemucs.yaml").write_text("dummy: true\n")
        if weights_bytes is not None:
            (snap / "955717e8.safetensors").write_bytes(b"x" * weights_bytes)
        return tmp_path

    def test_cached_true_when_weights_complete(self, monkeypatch, tmp_path):
        from video_translate import vocal_sep

        self._build_cache(tmp_path, weights_bytes=2048)
        monkeypatch.setattr(vocal_sep, "demucs_cache_dir", lambda: str(tmp_path))
        ok, path = cli._demucs_model_cached(min_bytes=1024)
        assert ok is True
        assert str(tmp_path) in path

    def test_cached_false_when_missing(self, monkeypatch, tmp_path):
        from video_translate import vocal_sep

        monkeypatch.setattr(vocal_sep, "demucs_cache_dir", lambda: str(tmp_path))
        ok, reason = cli._demucs_model_cached(min_bytes=1024)
        assert ok is False
        assert "missing" in reason.lower()

    def test_cached_false_when_truncated(self, monkeypatch, tmp_path):
        """Truncated download must NOT count as cached (E3 rule, applied to demucs)."""
        from video_translate import vocal_sep

        self._build_cache(tmp_path, weights_bytes=512)
        monkeypatch.setattr(vocal_sep, "demucs_cache_dir", lambda: str(tmp_path))
        ok, reason = cli._demucs_model_cached(min_bytes=1024)
        assert ok is False
        assert "incomplete" in reason.lower()

    def test_default_floor_rejects_small_weights(self, monkeypatch, tmp_path):
        """The 50 MB default floor is what catches a half-finished download."""
        from video_translate import vocal_sep

        self._build_cache(tmp_path, weights_bytes=1024 * 1024)  # 1 MB
        monkeypatch.setattr(vocal_sep, "demucs_cache_dir", lambda: str(tmp_path))
        ok, _reason = cli._demucs_model_cached()
        assert ok is False

    def test_doctor_prints_htdemucs_line(self, monkeypatch, capsys):
        _patch_all(monkeypatch, reachable=True)
        cli.cmd_doctor(_args())
        assert "htdemucs model cached" in capsys.readouterr().out

    def test_doctor_reports_miss_for_missing_weights(self, monkeypatch, capsys):
        _patch_all(monkeypatch, reachable=True, demucs_cached=False)
        cli.cmd_doctor(_args())
        assert "MISS" in capsys.readouterr().out

    def test_doctor_strict_with_missing_demucs_exits_7(self, monkeypatch):
        _patch_all(monkeypatch, reachable=True, demucs_cached=False)
        assert cli.cmd_doctor(_args(strict=True)) == cli.EXIT_DOCTOR_FAIL
