"""ffmpeg / ffprobe helpers.

Command construction is split from execution so it can be unit-tested without
invoking the binaries (see build_probe_cmd / build_extract_cmd).
"""
from __future__ import annotations

import subprocess


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
    proc = subprocess.run(build_probe_cmd(input_path), capture_output=True, text=True)
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
    subprocess.run(
        build_extract_cmd(input_path, wav_path, start, dur),
        capture_output=True, check=True,
    )


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
