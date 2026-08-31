"""Vocal / accompaniment separation preprocessing layer (T2, ADR-017, Spec 19).

Demucs is an optional heavy dependency. This module MUST be importable without
demucs/torchaudio installed — the base CLI paths (generate, verify, translate,
doctor without the flag) must not pay for it.

Hence:
  - ``demucs_available()`` does a lazy ``try: import demucs`` the first time
    and caches the result;
  - ``separate_vocals(...)`` returns None when the backend is unavailable
    (caller WARN + graceful fallback — Spec 19 invariant #5).
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .ffmpeg_utils import probe_duration
from .toolchain import resolve_tool, tool_available
from .io_utils import flush_print

# ---------------------------------------------------------------------------
# Lazy backend probe (NOTHING import demucs/torch at module toplevel)
# ---------------------------------------------------------------------------

_DEMUCS_AVAILABLE_CACHE: bool | None = None


# Project-local cache for the Demucs vocal-separation model (htdemucs).
#
# ADR-032 / Spec 26 — TWO download backends must be bound, not one:
#   * demucs >= 4.0 (what pyproject pins: ``demucs>=4.0.1``) downloads via
#     **huggingface_hub**, which honors HF_HOME — weights land in
#     ``HF_HOME/hub/models--adefossez--HTDemucs/``.
#   * demucs 3.x downloads via **torch.hub**, which honors TORCH_HOME — weights
#     land in ``TORCH_HOME/hub/checkpoints/``.
#
# The original implementation bound ONLY TORCH_HOME, on the assumption that
# Demucs used torch.hub. With demucs 4.x that was a no-op: the htdemucs weights
# silently landed in C:\Users\...\.cache\huggingface — exactly the system-drive
# artifact this rule exists to prevent (TOOLCHAIN §6 / MAJOR_VERSION_PLAN R5).
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_LOCAL_MODEL_DIR = os.path.join(_REPO_ROOT, "models")
_DEMUCS_CACHE_DIR = os.path.join(_LOCAL_MODEL_DIR, "torch")


def demucs_cache_dir() -> str:
    """Directory where the Demucs model is stored (project-local, not C:\\)."""
    return _DEMUCS_CACHE_DIR


def _bind_demucs_cache() -> None:
    """Point BOTH Demucs download backends at the project-local cache dir.

    ADR-032 / Spec 26 — sets ``HF_HOME`` (huggingface_hub, demucs >= 4.0) and
    ``TORCH_HOME`` (torch.hub, demucs 3.x) for the current process, so the
    htdemucs weights land in ``<repo>/models/torch`` instead of
    ``C:\\Users\\...\\.cache\\{huggingface,torch}``.

    Both are assigned unconditionally rather than only-when-unset: a stale
    system-cache env var on the host must be overridden, otherwise the C:-drive
    landing spot is silently re-introduced. Idempotent.
    """
    os.environ["HF_HOME"] = _DEMUCS_CACHE_DIR
    os.environ["TORCH_HOME"] = _DEMUCS_CACHE_DIR
    os.makedirs(_DEMUCS_CACHE_DIR, exist_ok=True)


def demucs_available() -> bool:
    """Lazy probe for the demucs package. Result is cached.

    Spec 19 § Interface — always safe to call, never raises ImportError.
    """
    global _DEMUCS_AVAILABLE_CACHE
    if _DEMUCS_AVAILABLE_CACHE is not None:
        return _DEMUCS_AVAILABLE_CACHE
    try:
        import demucs  # noqa: F401
        import torchaudio  # noqa: F401
        _DEMUCS_AVAILABLE_CACHE = True
    except Exception:
        _DEMUCS_AVAILABLE_CACHE = False
    return _DEMUCS_AVAILABLE_CACHE


# ---------------------------------------------------------------------------
# Fingerprint & cache paths
# ---------------------------------------------------------------------------


def _input_fingerprint(input_path: str) -> str:
    """sha1(abs_path + size + mtime)[:8].

    Changes when the file is replaced / edited — prevents reuse of a stale
    vocals.wav that corresponds to a previous version of the video.
    (Mirror of ADR-002 cache invalidation philosophy.)
    """
    p = Path(input_path).resolve()
    try:
        st = p.stat()
        size = st.st_size
        mtime = int(st.st_mtime * 1000)
    except OSError:
        size = 0
        mtime = 0
    raw = f"{str(p)}|{size}|{mtime}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:8]


def separate_fingerprint(
    input_path: str,
    backend: str = "demucs",
    model_name: str = "htdemucs",
) -> str:
    """Content hash of everything that influences the separation output.

    Output: 8 hex chars. Spec 19 § Interface.
    """
    payload: dict[str, Any] = {
        "input_hash": _input_fingerprint(input_path),
        "backend": backend,
        "model": model_name,
        "output_sr": 16000,
        "output_ch": 1,
        "version": 1,  # bump when algorithm changes → force cache invalidation
    }
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()[:8]


def vocals_wav_path(outdir: str, base: str, fp: str) -> str:
    """Naming convention: {outdir}/{base}.{fp}.vocals.wav"""
    return str(Path(outdir) / f"{base}.{fp}.vocals.wav")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Three-lane vocal separation route (ADR-031 / Spec 25)
# ---------------------------------------------------------------------------
# Demucs used to pick its device with a single binary choice: cuda if torch saw
# a CUDA device, else cpu. That caused two distinct failures:
#
#   * a machine with an NVIDIA card whose torch was installed as a CPU wheel
#     silently ran Demucs on the CPU — a window that takes seconds on CUDA
#     takes close to an hour on CPU, which is indistinguishable from a hang;
#   * plugging that hole with "no CUDA => never run Demucs" then lumped three
#     genuinely different machines (NVIDIA / Apple Silicon / plain CPU) into a
#     single disabled branch, permanently disabling separation on Apple Silicon.
#
# The route below separates the two questions that used to be conflated:
#
#   HARDWARE — what kind of machine is this?  (never depends on torch)
#   RUNTIME  — is this machine's GPU stack usable right now?
#
# and evaluates them in an order that makes the red line structural: once an
# NVIDIA card is detected, the Apple Silicon / CPU lanes are never consulted, so
# a broken CUDA install can only ever yield "not ready" — never a downgrade.

VSEP_LANE_CUDA = "cuda"
VSEP_LANE_APPLE_SILICON = "apple_silicon"
VSEP_LANE_CPU = "cpu"


@dataclass(frozen=True)
class VsepRoute:
    """Immutable result of the vocal-separation lane decision (Spec 25).

    lane
        "cuda" | "apple_silicon" | "cpu" — the hardware lane.
    device
        "cuda" or None — the value handed to ``demucs -d``. None whenever the
        lane must not run Demucs at all.
    can_separate
        Whether Demucs may run on this machine right now.
    message
        Why it cannot (empty when ``can_separate``). Consumers MUST print it —
        silent degradation is precisely what this design exists to prevent.
    """

    lane: str
    device: str | None
    can_separate: bool
    message: str


# Cached: the route is consulted by both the CLI gate and ``separate_vocals``,
# and re-probing would re-import torch (a heavy, avoidable cost).
_vsep_route_cache: VsepRoute | None = None


def _is_nvidia_hardware() -> bool:
    """True when an NVIDIA card is present (nvidia-smi reachable on PATH).

    Deliberately torch-independent: a broken or CPU-only torch install must not
    be able to make an NVIDIA machine look like a CPU machine.
    """
    return tool_available("nvidia-smi")


def _is_apple_silicon() -> bool:
    """True on Apple Silicon (darwin + arm64).

    Intel Macs (darwin + x86_64) and Linux arm64 are NOT Apple Silicon — they
    fall through to the CPU lane. A Rosetta interpreter also reports x86_64,
    which is correct: its torch build cannot reach MPS either.
    """
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _torch_cuda_ready() -> bool:
    """True only when torch is importable AND reports a usable CUDA device.

    Never raises — a missing or broken torch simply means "not ready".
    """
    try:
        import torch  # lazy — keeps base CLI importable without a heavy import
    except Exception:
        return False
    try:
        return bool(torch.cuda.is_available() and torch.cuda.device_count() > 0)
    except Exception:
        return False


def resolve_vsep_route() -> VsepRoute:
    """Decide which vocal-separation lane this machine belongs to (Spec 25).

    Evaluation order is the red-line guarantee: NVIDIA hardware is probed FIRST
    and locks the lane in. An NVIDIA machine whose CUDA stack is broken can
    therefore only ever report "not ready" — it can never be re-classified as
    Apple Silicon or CPU and then run Demucs on CPU/MPS.

    The result is cached for the process lifetime.
    """
    global _vsep_route_cache
    if _vsep_route_cache is not None:
        return _vsep_route_cache

    if _is_nvidia_hardware():
        route = (
            VsepRoute(lane=VSEP_LANE_CUDA, device="cuda",
                      can_separate=True, message="")
            if _torch_cuda_ready()
            else VsepRoute(
                lane=VSEP_LANE_CUDA,
                device=None,
                can_separate=False,
                message=(
                    "检测到 NVIDIA GPU，但 CUDA 运行时未就绪"
                    "（torch 装成了 CPU wheel，或环境还没初始化）。"
                    "请运行 `make setup` —— uv sync 会自动安装 CUDA 版 torch。"
                    "人声分离绝不会降级到 CPU 执行。"
                ),
            )
        )
    elif _is_apple_silicon():
        route = VsepRoute(
            lane=VSEP_LANE_APPLE_SILICON,
            device=None,
            can_separate=False,
            message=(
                "Apple Silicon 上的人声分离目前是待办（TODO）：MPS 与 CUDA 是"
                "两套独立后端，且 htdemucs 在 MPS 上未必比 CPU 快，本期未实现。"
                "不会降级到 CPU 执行。"
            ),
        )
    else:
        route = VsepRoute(
            lane=VSEP_LANE_CPU,
            device=None,
            can_separate=False,
            message=(
                "本机是纯 CPU 环境，不支持人声分离"
                "（Demucs 属 GPU-only 能力）。"
            ),
        )

    _vsep_route_cache = route
    return route


def _extract_vocals_via_cli(
    input_path: str,
    tmpdir: str,
    model_name: str,
    device: str,
) -> str | None:
    """Call ``demucs`` CLI as a subprocess; returns the raw vocals wav path.

    We fall back to the CLI when the Python API surface isn't stable. The CLI
    guarantees:
      * output length == input length (Demucs invariant we rely on)
      * two-step process: demucs writes {model}/{stem}/{name}.wav, then we
        resample with ffmpeg to 16kHz mono.

    Returns abs path to the DEMUCS-PRODUCED wav (raw sr, usually 44100 stereo),
    or None on failure.
    """
    if not tool_available("demucs"):
        return None
    demucs_bin = resolve_tool("demucs")
    out_tmp = Path(tmpdir) / "demucs_out"
    out_tmp.mkdir(parents=True, exist_ok=True)
    name = Path(input_path).stem
    cmd = [
        demucs_bin,
        "--two-stems", "vocals",
        "-n", model_name,
        "-d", device,
        "-o", str(out_tmp),
        input_path,
    ]
    # Stream Demucs' own output instead of capturing it. A separation can take
    # minutes per window; swallowing its progress made healthy runs look frozen
    # (the terminal showed one line and then nothing until the window finished).
    started = time.monotonic()
    try:
        subprocess.run(cmd, check=True, timeout=None)
    except Exception:
        return None
    finally:
        flush_print(f"[vsep] demucs finished in {time.monotonic() - started:.1f}s")
    # Convention: demucs -o dir/ input → dir/{model}/{stem}/{name}.wav
    produced = out_tmp / model_name / name / "vocals.wav"
    if not produced.is_file():
        return None
    return str(produced)


def _resample_to_16k_mono(src: str, dst: str) -> bool:
    """FFmpeg resample to 16kHz mono WAV (Whisper expected input format)."""
    ffmpeg_bin = resolve_tool("ffmpeg")
    if not ffmpeg_bin:
        return False
    cmd = [
        ffmpeg_bin, "-y", "-v", "error", "-nostdin",
        "-i", src,
        "-ar", "16000", "-ac", "1",
        "-f", "wav", dst,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except Exception:
        return False
    return Path(dst).is_file()


def separate_vocals(
    input_path: str,
    outdir: str,
    *,
    base: str | None = None,
    backend: str = "demucs",
    model_name: str = "htdemucs",
    device: str = "auto",
    progress: Callable[..., None] = flush_print,
) -> str | None:
    """Separate vocals from input, cache to ``{outdir}/{base}.{fp}.vocals.wav``.

    Returns the 16kHz mono WAV absolute path, or None if the backend is
    unavailable (caller should WARN + fall back to original audio).

    Spec 19 § Algorithm:
      * fp = separate_fingerprint(...)
      * cache hit on vocals.wav + duration matches input → reuse
      * otherwise separate via demucs, resample, release GPU memory, return.
    """
    if base is None:
        base = Path(input_path).stem
    Path(outdir).mkdir(parents=True, exist_ok=True)

    if backend != "demucs":
        # Only one backend implemented for now; caller should WARN and fallback
        return None

    fp = separate_fingerprint(input_path, backend, model_name)
    vocals_path = vocals_wav_path(outdir, base, fp)

    # ------------------------------------------------------------------
    # Cache hit check + duration-assertion invariant (ADR-017 §2).
    # This is THE load-bearing guard against shifted timestamps.
    # ------------------------------------------------------------------
    if Path(vocals_path).is_file() and Path(vocals_path).stat().st_size > 0:
        try:
            dur_in = probe_duration(input_path)
            dur_sep = probe_duration(vocals_path)
            if abs(dur_in - dur_sep) < 0.05:
                progress(f"[skip] vocals cached ({dur_sep:.1f}s, reuse)")
                return vocals_path
            # else: stale cache (length mismatch — Demucs output got corrupted
            # or the input video was re-muxed to a different duration); remove
            # and redo.
            progress(f"[warn] cached vocals duration mismatch "
                     f"({dur_sep:.3f}s vs input {dur_in:.3f}s); discarding cache")
            try:
                os.remove(vocals_path)
            except OSError:
                pass
        except Exception:
            # probe_duration failed — can't verify; play safe and redo
            try:
                os.remove(vocals_path)
            except OSError:
                pass

    if not demucs_available():
        return None  # graceful fallback (Spec 19 invariant #5)

    # Bind Demucs' model download (huggingface_hub on 4.x, torch.hub on 3.x) to
    # the project-local cache so the htdemucs weights land in <repo>/models/torch,
    # never C:\Users\...\.cache. See ADR-032 / Spec 26.
    _bind_demucs_cache()

    # Hard gate (Spec 25 / ADR-031): Demucs runs only on a lane that can host
    # it. Every other lane refuses loudly and returns None so the caller falls
    # back to the original audio — the old "silently pick cpu" behaviour turned
    # a minutes-long job into an hour-long apparent hang.
    route = resolve_vsep_route()
    if not route.can_separate:
        progress(f"[vsep] vocal separation unavailable: {route.message} "
                 "Skipping separation — falling back to the original audio.")
        return None
    dev = route.device
    if not dev:
        # Defensive: can_separate=True always carries a device. Keeps the type
        # checker honest without weakening the gate above.
        progress("[vsep] internal error: route allows separation but carries no "
                 "device; skipping separation.")
        return None
    if device and device != "auto" and device != dev:
        # The lane route is the single truth source (Spec 25). Honouring a
        # caller-supplied device that contradicts it would re-open the
        # silent-CPU-downgrade hole, so it is ignored rather than applied.
        progress(f"[vsep] ignoring requested device {device!r}: this machine's "
                 f"route mandates {dev!r}.")
    progress(f"[vsep] separating vocals with {backend}/{model_name} ({dev}) …")

    # We use a temp dir for demucs's raw multi-channel output, then resample
    # once to the final 16kHz mono target. Keeping the raw artifacts out of
    # the user's video dir avoids clutter.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="vsep_") as tmpdir:
        raw_vocals = _extract_vocals_via_cli(input_path, tmpdir, model_name, dev)
        if raw_vocals is None:
            progress(f"[warn] demucs CLI failed; falling back to original audio")
            return None
        # 2. Resample to Whisper's expected format (16k mono wav)
        ok = _resample_to_16k_mono(raw_vocals, vocals_path)
        if not ok:
            return None

    # ------------------------------
    # Explicit GPU memory release
    # ------------------------------
    try:
        import torch  # type: ignore
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    # Final invariant check before we hand this off to transcribe:
    try:
        dur_in = probe_duration(input_path)
        dur_out = probe_duration(vocals_path)
        if abs(dur_in - dur_out) >= 0.05:
            progress(f"[error] demucs output duration mismatch after resample "
                     f"(input={dur_in:.3f}s vocals={dur_out:.3f}s); discarding")
            try:
                os.remove(vocals_path)
            except OSError:
                pass
            return None
    except Exception:
        # probe failed; conservative — do not trust the file
        try:
            os.remove(vocals_path)
        except OSError:
            pass
        return None

    progress(f"[vsep] vocals ready: {Path(vocals_path).name} "
             f"({probe_duration(vocals_path):.1f}s)")
    return vocals_path