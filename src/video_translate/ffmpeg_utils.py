"""ffmpeg / ffprobe helpers.

Command construction is split from execution so it can be unit-tested without
invoking the binaries (see build_probe_cmd / build_extract_cmd).
"""
from __future__ import annotations

import subprocess


def _resolve_binary(name: str) -> str:
    """Resolve a tool (ffmpeg/ffprobe/demucs/…) to its **persisted, absolute** path.

    Single source of truth: ``toolchain.resolve_tool()``, which reads the path
    persisted by ``init_toolchain`` (E2 — resolved once at startup from
    ``.env(.local)`` / ``VT_FFMPEG_DIR`` and cached in ``_GLOBAL_TOOLCHAIN``).
    Every downstream tool call reuses that exact absolute path, so a binary
    found at one pipeline stage is never "lost" at a later one (fill_gaps /
    verify) — no per-call PATH search, no dependence on the current working
    directory.

    This used to be a second, hand-rolled copy of the resolver with hard-coded
    ``if name == "ffmpeg"`` branches, so any tool registered later in
    ``toolchain._TOOL_REGISTRY`` was invisible here — the gap that let demucs /
    nvidia-smi lookups drift between stages.

    Direct calls (e.g. ``analyze_audio`` / ``verify`` from a plain script that
    bypasses the CLI's ``init_toolchain``) also self-heal: the first resolution
    auto-loads the ``.env(.local)`` config and binds the portable build, instead
    of raising FileNotFoundError. Command *construction* stays pure
    (unit-tested); resolution happens only at execution time.

    Fallback chain (preserves system-PATH / bare-script usability):
      1. cached toolchain status path (absolute — preferred)
      2. ``shutil.which(name)`` — system-installed binary on PATH
      3. bare name — subprocess searches PATH at exec time
    """
    try:
        from .toolchain import resolve_tool

        return resolve_tool(name)
    except Exception:  # noqa: BLE001 - never let resolution crash a tool call
        # Bare name: subprocess searches PATH at exec time. `resolve_tool` already
        # carries the shutil.which fallback, so duplicating it here would be a
        # second resolver that drifts from the registry.
        return name


def build_probe_cmd(input_path: str) -> list[str]:
    """Build the ffprobe command that prints media duration in seconds."""
    return [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_path,
    ]


def build_extract_cmd(input_path: str, wav_path: str, start: float, dur: float) -> list[str]:
    """Build the ffmpeg command that extracts a 16kHz mono WAV chunk.

    16kHz mono is what Whisper expects; extracting per-chunk keeps peak disk/mem low.
    """
    return [
        "ffmpeg", "-y",
        "-ss", str(start), "-t", str(dur),
        "-i", input_path,
        "-ar", "16000", "-ac", "1", "-f", "wav",
        wav_path,
    ]


def probe_duration(input_path: str) -> float:
    """Return media duration in seconds via ffprobe.

    Raises:
        RuntimeError: if ffprobe fails or returns unparseable output.
    """
    cmd = build_probe_cmd(input_path)
    cmd[0] = _resolve_binary("ffprobe")
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        # Explicit utf-8: on Windows `text=True` decodes with the locale codec
        # (GBK on zh-CN), which cannot decode a non-ASCII path echoed by ffprobe.
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {input_path!r}: {proc.stderr.strip()[:200]}")
    out = proc.stdout.strip()
    try:
        return float(out)
    except ValueError as e:
        raise RuntimeError(f"ffprobe returned non-numeric duration {out!r}") from e


def extract_chunk(input_path: str, wav_path: str, start: float, dur: float) -> None:
    """Extract a WAV chunk [start, start+dur) to `wav_path`.

    Raises:
        subprocess.CalledProcessError: if ffmpeg fails.
    """
    cmd = build_extract_cmd(input_path, wav_path, start, dur)
    cmd[0] = _resolve_binary("ffmpeg")
    subprocess.run(cmd, capture_output=True, check=True)


def build_audio_profile_cmd(input_path: str, noise: str = "-30dB", d: float = 0.3) -> list[str]:
    """Build the ffmpeg command that runs volumedetect + silencedetect in one pass.

    Both filters log to stderr; the caller parses it (see
    ``video_translate.audio_profile``). No output file is written (`-f null -`).
    """
    return [
        "ffmpeg", "-hide_banner", "-nostats", "-i", input_path,
        "-af", f"volumedetect,silencedetect=noise={noise}:d={d}",
        "-f", "null", "-",
    ]
