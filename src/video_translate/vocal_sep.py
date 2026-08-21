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
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .ffmpeg_utils import probe_duration

# ---------------------------------------------------------------------------
# Lazy backend probe (NOTHING import demucs/torch at module toplevel)
# ---------------------------------------------------------------------------

_DEMUCS_AVAILABLE_CACHE: bool | None = None


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


def _auto_device() -> str:
    """Pick cuda if the runtime has a working CUDA device, else cpu."""
    try:
        import torch  # lazy — keeps base CLI importable without a heavy torch import
    except Exception:
        return "cpu"
    try:
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            return "cuda"
    except Exception:
        pass
    return "cpu"


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
    demucs_bin = shutil_which("demucs")
    if demucs_bin is None:
        return None
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
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=None)
    except Exception:
        return None
    # Convention: demucs -o dir/ input → dir/{model}/{stem}/{name}.wav
    produced = out_tmp / model_name / name / "vocals.wav"
    if not produced.is_file():
        return None
    return str(produced)


def shutil_which(exe: str) -> str | None:
    """Small shutil.which wrapper for test patching."""
    import shutil
    return shutil.which(exe)


def _resample_to_16k_mono(src: str, dst: str) -> bool:
    """FFmpeg resample to 16kHz mono WAV (Whisper expected input format)."""
    ffmpeg_bin = os.environ.get("VT_FFMPEG") or _which_or_ffmpeg("ffmpeg")
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


def _which_or_ffmpeg(name: str) -> str:
    import shutil
    return shutil.which(name) or name


def separate_vocals(
    input_path: str,
    outdir: str,
    *,
    base: str | None = None,
    backend: str = "demucs",
    model_name: str = "htdemucs",
    device: str = "auto",
    progress: Callable[..., None] = print,
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

    dev = device if device and device != "auto" else _auto_device()
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