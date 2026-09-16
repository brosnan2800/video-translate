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

ADR-038 D7（能力分层）: 能力分**通用**（``common=True``：ffmpeg / ffprobe，与引擎
无关，阶段表可静态声明）与**引擎特定**（模型 / CUDA / whisperx / demucs，由 Provider
的 ``prerequisites()`` 自报、在组装 ctx 时注入）。``model:<name>`` 型 id 由前缀解析
而非逐个注册——换引擎时本模块无需改动。
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
    # core = the control plane enforces it by default。**硬前置**（缺失即无法推进本
    # 阶段，如 ffmpeg / ffprobe / 模型）保持 True；**可选能力**（cuda / whisperx /
    # demucs —— CPU 可跑、可降级、按 flag 启用）标 False：它们照常出现在 doctor 的
    # 体检里，但不作为闸门的「阻断项」（否则 CPU / macOS 的 NEXT 块会长期挂误导 MISS）。
    core: bool = True
    # ADR-038 D7 通用前置：与 ASR 引擎无关的就绪要求（ffmpeg / ffprobe 是所有引擎的
    # 共同前置，同时服务 verify 的独立验证）。阶段表（pipeline_def.STAGES）**只**
    # 声明这类；引擎特定前置由 Provider 的 prerequisites() 自报，组装 ctx 时注入。
    common: bool = False


def _probe_ffmpeg() -> bool:
    from .toolchain import tool_available

    return tool_available("ffmpeg")


def _probe_ffprobe() -> bool:
    from .toolchain import tool_available

    return tool_available("ffprobe")


def _probe_model_large_v3() -> bool:
    """large-v3 是否已就位（E3 完整性口径）。

    实现下沉到 ``model_cache``：此前这里 import ``cli._model_cached``，使
    「控制面通用层」反向依赖「入口壳」（靠延迟 import 勉强绕过循环）——ADR-038 D7
    的 `model:` 类能力由本模块**按 id 解析**，不再写死具体模型名。
    """
    from .model_cache import model_cached

    return model_cached("large-v3")


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
        common=True,
    ),
    Capability(
        "ffprobe",
        _probe_ffprobe,
        "ffprobe missing (ships together with ffmpeg). Run "
        "`video-translate setup --ffmpeg`.",
        common=True,
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
        core=False,  # 可选：CPU 路径可用（仅加速项）
    ),
    Capability(
        "whisperx",
        _probe_whisperx,
        "WhisperX unavailable (needs NVIDIA CUDA + package). On a CUDA host "
        "run `uv sync --extra gpu`. macOS has no whisperx path.",
        core=False,  # 可选：--align auto 会降级 none
    ),
    Capability(
        "demucs",
        _probe_demucs,
        "demucs not installed (core dependency). Run `uv sync` "
        "(`pip install -e .` fallback) — never `pip install` torch ad-hoc.",
        core=False,  # 可选：仅 --separate-vocals 需要
    ),
)

_CAP_BY_NAME: dict[str, Capability] = {c.name: c for c in CAPS}

# ADR-038 D7: `model:<name>` 型能力 id 由**前缀解析**，不逐个静态注册——新引擎
# 声明 `model:sensevoice` 时本模块无需改动（命名即契约）。探测实现复用
# model_cache 的 E3 完整性口径（与 setup 自愈同源）。
_MODEL_PREFIX = "model:"


def _model_guidance(name: str) -> str:
    return (
        f"model '{name[len(_MODEL_PREFIX):]}' is not available. Run "
        "`video-translate setup` (pre-downloads into <repo>/models — never the "
        "system drive; truncated downloads self-heal), or drop the model dir "
        "into <repo>/models/<name> manually."
    )


def capability(name: str) -> Capability | None:
    """Look up a capability definition; synthesize unregistered `model:<name>` ids.

    ADR-038 D7：Provider 自报的 `model:*` 无需事先在 :data:`CAPS` 注册即可探测与
    取修复指引——引擎侧只负责声明 id（命名即契约）。
    """
    cap = _CAP_BY_NAME.get(name)
    if cap is not None:
        return cap
    if name.startswith(_MODEL_PREFIX) and len(name) > len(_MODEL_PREFIX):
        from .model_cache import model_cached

        model_name = name[len(_MODEL_PREFIX):]
        return Capability(name, lambda: model_cached(model_name),
                          _model_guidance(name))
    return None


def guidance_for(name: str) -> str:
    """Repair guidance for a capability id ("" when the id is unknown)."""
    cap = capability(name)
    return cap.guidance if cap is not None else ""


def common_cap_names() -> tuple[str, ...]:
    """通用前置（ADR-038 D7）：与 ASR 引擎无关，阶段表可静态声明。"""
    return tuple(c.name for c in CAPS if c.common)


def probe(name: str) -> bool:
    """Run a single capability probe (never raises for known names)."""
    cap = capability(name)
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
    cap = capability(name)
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
