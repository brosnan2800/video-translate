"""Capability probes + gate-failure type (control plane, §2).

Single source of environment-availability truth for every gate in the control
plane (P0 preflight / ``enforce()``). Each capability is a probe function
(reusing existing detection — never re-implemented ad-hoc) plus deterministic
repair guidance shown on hard-stop.

Design (see ADR-030 §2 — 原 CONTROL-PLANE-PLAN 已归档至 docs/archive/):
  - ``doctor`` keeps its own rendering for output compatibility; the control
    plane consumes ``CAPS`` / ``probe()`` directly.
  - ``GateFail`` is raised by domain code when an EXPLICIT request cannot be
    honored; ``cli.main()`` catches it and exits ``EXIT_GATE_FAIL`` (8). An
    implicit/default request that can't be honored degrades with a logged
    reason instead (裁决一: explicit must be honored, auto may degrade).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class GateFail(RuntimeError):
    """Raised when an explicit user decision cannot be honored (→ exit 8).

    Carries the deterministic repair guidance; ``cli.main()`` catches it, prints
    the guidance, and returns ``EXIT_GATE_FAIL`` — never silently degrades an
    explicit intent.
    """

    def __init__(self, message: str, guidance: str = ""):
        super().__init__(message)
        self.message = message
        self.guidance = guidance


@dataclass(frozen=True)
class Capability:
    name: str
    probe: Callable[[], bool]
    guidance: str
    core: bool = True  # core = the control plane enforces it by default


def _probe_ffmpeg() -> bool:
    from .toolchain import tool_available

    return tool_available("ffmpeg")


def _probe_ffprobe() -> bool:
    from .toolchain import tool_available

    return tool_available("ffprobe")


def _probe_model_large_v3() -> bool:
    # Deferred import: cli imports capabilities, so importing cli here top-level
    # would be a cycle. At call time cli is always already loaded.
    from .cli import _model_cached

    return _model_cached("large-v3")


def _probe_cuda() -> bool:
    from .toolchain import get_toolchain_status

    return get_toolchain_status().cuda_available


def _probe_whisperx() -> bool:
    from .align import whisperx_available

    return whisperx_available()


def _probe_demucs() -> bool:
    from .vocal_sep import demucs_available

    return demucs_available()


CAPS: tuple[Capability, ...] = (
    Capability(
        "ffmpeg",
        _probe_ffmpeg,
        "ffmpeg missing. Run `video-translate setup --ffmpeg` (downloads the "
        "portable build into tools/ffmpeg; no manual install, no scatter).",
    ),
    Capability(
        "ffprobe",
        _probe_ffprobe,
        "ffprobe missing (ships together with ffmpeg). Run "
        "`video-translate setup --ffmpeg`.",
    ),
    Capability(
        "model:large-v3",
        _probe_model_large_v3,
        "large-v3 model missing. Run `video-translate setup` (pre-downloads "
        "~3GB into <repo>/models, no C:\\ cache; truncated downloads self-heal).",
    ),
    Capability(
        "cuda",
        _probe_cuda,
        "NVIDIA CUDA runtime not detected — heavy stages (whisperx alignment, "
        "demucs) cannot run. Install CUDA / use a GPU host, or fall back to "
        "backends that support CPU.",
    ),
    Capability(
        "whisperx",
        _probe_whisperx,
        "WhisperX unavailable (needs NVIDIA CUDA + package). On a CUDA host "
        "run `uv sync --extra gpu`. macOS has no whisperx path.",
    ),
    Capability(
        "demucs",
        _probe_demucs,
        "demucs not installed (core dependency). Run `uv sync` "
        "(`pip install -e .` fallback) — never `pip install` torch ad-hoc.",
    ),
)

_CAP_BY_NAME: dict[str, Capability] = {c.name: c for c in CAPS}


def probe(name: str) -> bool:
    """Run a single capability probe (never raises for known names)."""
    cap = _CAP_BY_NAME.get(name)
    if cap is None:
        raise GateFail(f"unknown capability {name!r}", "")
    try:
        return bool(cap.probe())
    except Exception:  # noqa: BLE001 - an unrunnable probe must fail closed
        return False


def probe_all() -> dict[str, bool]:
    """Snapshot of every capability's availability (for state file + status)."""
    return {c.name: probe(c.name) for c in CAPS}


def require(name: str) -> None:
    """Hard-stop guard: raise GateFail when the capability is unavailable."""
    cap = _CAP_BY_NAME.get(name)
    if cap is None:
        raise GateFail(f"unknown capability {name!r}", "")
    if not probe(name):
        raise GateFail(
            f"required capability '{name}' is unavailable", cap.guidance
        )


def require_all(core_only: bool = True) -> None:
    """Require every core capability (the pipeline can't run without them)."""
    for c in CAPS:
        if (not core_only) or c.core:
            require(c.name)
