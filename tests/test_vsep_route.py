"""TDD tests for the three-lane vocal separation route (ADR-031 / Spec 25).

These lock the routing contract down BEFORE any consumer is wired up:

  * three independent lanes — CUDA / Apple Silicon (TODO) / CPU;
  * one single truth source consumed by doctor + every separation gate;
  * the red line: a machine with an NVIDIA card must NEVER be routed to the
    Apple Silicon or CPU lane, no matter how broken its torch install is.

Every platform / torch probe is mocked, so these run on any machine.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_route_cache():
    """`resolve_vsep_route` caches its result — clear it around every test."""
    from video_translate import vocal_sep

    vocal_sep._vsep_route_cache = None
    yield
    vocal_sep._vsep_route_cache = None


# ---------------------------------------------------------------------------
# Routing decisions (Spec 25 § 判定矩阵)
# ---------------------------------------------------------------------------


class TestResolveVsepRoute:
    """Spec 25 § 判定矩阵 — the four routing outcomes."""

    @staticmethod
    def _resolve(nvidia: bool, apple: bool, cuda_ready: bool):
        """Resolve a route with the three probes stubbed out."""
        from video_translate import vocal_sep

        with patch.object(vocal_sep, "_is_nvidia_hardware", return_value=nvidia), \
             patch.object(vocal_sep, "_is_apple_silicon", return_value=apple), \
             patch.object(vocal_sep, "_torch_cuda_ready", return_value=cuda_ready):
            return vocal_sep.resolve_vsep_route()

    def test_nvidia_with_cuda_ready_runs_on_cuda(self):
        """① N 卡 + CUDA 就绪 → 唯一允许执行 Demucs 的路径。"""
        r = self._resolve(nvidia=True, apple=False, cuda_ready=True)
        assert r.lane == "cuda"
        assert r.device == "cuda"
        assert r.can_separate is True
        assert r.message == ""

    def test_nvidia_without_cuda_stays_on_cuda_lane(self):
        """② 红线：N 卡但 CUDA 未就绪 → 仍锁在 cuda 线，绝不降级 CPU/MPS。"""
        r = self._resolve(nvidia=True, apple=False, cuda_ready=False)
        assert r.lane == "cuda", (
            "RED LINE: an NVIDIA machine must stay on the cuda lane even when "
            "torch reports no CUDA — it must never fall through to cpu/mps"
        )
        assert r.can_separate is False
        assert r.device is None, "must not hand a cpu/mps device to demucs"
        # The message must tell the user how to FIX it, not just that it broke.
        assert "setup" in r.message.lower()
        assert "cuda" in r.message.lower()

    def test_apple_silicon_is_todo_lane(self):
        """③ darwin + arm64 → 待办线，不降级 CPU。"""
        r = self._resolve(nvidia=False, apple=True, cuda_ready=False)
        assert r.lane == "apple_silicon"
        assert r.can_separate is False
        assert r.device is None
        assert "待办" in r.message or "todo" in r.message.lower()

    def test_plain_cpu_lane(self):
        """④ 无 N 卡、非 arm64 → CPU 线，不支持人声分离。"""
        r = self._resolve(nvidia=False, apple=False, cuda_ready=False)
        assert r.lane == "cpu"
        assert r.can_separate is False
        assert r.device is None
        assert r.message

    def test_apple_silicon_flag_ignored_when_nvidia_present(self):
        """红线补强：即便平台探测误报 arm64，N 卡优先级更高。"""
        r = self._resolve(nvidia=True, apple=True, cuda_ready=True)
        assert r.lane == "cuda"

    def test_route_is_frozen(self):
        """Route result must be immutable (Spec 25 § 模块接口)。"""
        import dataclasses

        r = self._resolve(nvidia=True, apple=False, cuda_ready=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.lane = "cpu"  # type: ignore[misc]

    def test_result_is_cached_and_short_circuits(self):
        """缓存：重复调用只算一次；且命中 N 卡后不再探测 Apple Silicon。"""
        from video_translate import vocal_sep

        with patch.object(vocal_sep, "_is_nvidia_hardware", return_value=True) as m_nv, \
             patch.object(vocal_sep, "_is_apple_silicon", return_value=False) as m_ap, \
             patch.object(vocal_sep, "_torch_cuda_ready", return_value=True) as m_cu:
            first = vocal_sep.resolve_vsep_route()
            second = vocal_sep.resolve_vsep_route()

        assert first is second, "cached route must be the same object"
        assert m_nv.call_count == 1
        assert m_cu.call_count == 1
        assert m_ap.call_count == 0, (
            "once the NVIDIA lane is locked in, later lanes must not be probed"
        )


# ---------------------------------------------------------------------------
# Probes (hardware / runtime detection)
# ---------------------------------------------------------------------------


class TestProbes:
    """Spec 25 § 模块接口 — the three underlying probes never raise."""

    def test_is_nvidia_hardware_follows_nvidia_smi(self):
        from video_translate import vocal_sep

        with patch.object(vocal_sep, "shutil_which", return_value="/usr/bin/nvidia-smi"):
            assert vocal_sep._is_nvidia_hardware() is True
        with patch.object(vocal_sep, "shutil_which", return_value=None):
            assert vocal_sep._is_nvidia_hardware() is False

    def test_is_apple_silicon_requires_darwin_and_arm64(self):
        from video_translate import vocal_sep

        with patch.object(sys, "platform", "darwin"), \
             patch("platform.machine", return_value="arm64"):
            assert vocal_sep._is_apple_silicon() is True

        # Intel Mac: darwin but x86_64 → NOT Apple Silicon (falls to CPU lane).
        with patch.object(sys, "platform", "darwin"), \
             patch("platform.machine", return_value="x86_64"):
            assert vocal_sep._is_apple_silicon() is False

        # Linux arm64 is not Apple Silicon either.
        with patch.object(sys, "platform", "linux"), \
             patch("platform.machine", return_value="arm64"):
            assert vocal_sep._is_apple_silicon() is False

    def test_torch_cuda_ready_false_when_torch_missing(self):
        """torch 未安装 / import 失败 → False，绝不抛异常。"""
        from video_translate import vocal_sep

        with patch.dict(sys.modules, {"torch": None}):
            assert vocal_sep._torch_cuda_ready() is False

    def test_torch_cuda_ready_reflects_runtime(self):
        from video_translate import vocal_sep

        ready = MagicMock()
        ready.cuda.is_available.return_value = True
        ready.cuda.device_count.return_value = 1
        with patch.dict(sys.modules, {"torch": ready}):
            assert vocal_sep._torch_cuda_ready() is True

        # is_available() but zero devices — a broken/partial CUDA runtime.
        no_dev = MagicMock()
        no_dev.cuda.is_available.return_value = True
        no_dev.cuda.device_count.return_value = 0
        with patch.dict(sys.modules, {"torch": no_dev}):
            assert vocal_sep._torch_cuda_ready() is False

        # torch installed but CPU-only wheel.
        cpu_only = MagicMock()
        cpu_only.cuda.is_available.return_value = False
        with patch.dict(sys.modules, {"torch": cpu_only}):
            assert vocal_sep._torch_cuda_ready() is False
